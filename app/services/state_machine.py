"""
Workload state machine enforcer.

Centralizes all state transition logic and ensures every transition
is validated and recorded in the workload_events audit log.

Usage (in Celery tasks — synchronous context):
    from app.services.state_machine import transition_workload_state

    transition_workload_state(
        db=db,
        workload_id="wl-20260601-abc123",
        new_state="VALIDATING",
        trigger="validate_node",
        message="Starting GPU validation on 10.6.12.15",
    )
"""

import logging
from sqlalchemy.orm import Session

from app.models.workload import Workload, WorkloadState
from app.models.workload_event import WorkloadEvent

logger = logging.getLogger(__name__)

# ── Valid Transitions ─────────────────────────────────────────────────────────
# Maps current_state → set of allowed next states.
# FAILED is reachable from ANY state (handled separately below).
VALID_TRANSITIONS: dict[str, set[str]] = {
    WorkloadState.CREATED:    {WorkloadState.VALIDATING, WorkloadState.FAILED},
    WorkloadState.VALIDATING: {WorkloadState.VALIDATED,  WorkloadState.FAILED},
    WorkloadState.VALIDATED:  {WorkloadState.INSTALLING, WorkloadState.FAILED},
    WorkloadState.INSTALLING: {WorkloadState.READY,      WorkloadState.FAILED},
    WorkloadState.READY:      {WorkloadState.RUNNING,    WorkloadState.FAILED},
    WorkloadState.RUNNING:    {WorkloadState.READY,      WorkloadState.FAILED},
    WorkloadState.FAILED:     set(),  # terminal — no transitions out
}


class InvalidStateTransition(Exception):
    """Raised when a state transition violates the state machine rules."""
    pass


def transition_workload_state(
    db: Session,
    workload_id: str,
    new_state: str,
    trigger: str,
    message: str,
) -> None:
    """
    Atomically transitions a workload to a new state and writes an audit event.

    Args:
        db:           A synchronous SQLAlchemy session (used inside Celery tasks).
        workload_id:  The human-readable workload_id (e.g. "wl-20260601-abc123").
        new_state:    Target state (must be a valid WorkloadState constant).
        trigger:      Name of the Celery task or action causing this transition.
        message:      Human-readable message for the audit log / UI stream.

    Raises:
        ValueError:              If the workload is not found.
        InvalidStateTransition:  If the transition is not allowed.
    """
    workload = (
        db.query(Workload)
        .filter(Workload.workload_id == workload_id)
        .with_for_update()  # row-level lock to prevent concurrent transitions
        .first()
    )

    if not workload:
        raise ValueError(f"Workload '{workload_id}' not found")

    current_state = workload.state
    allowed = VALID_TRANSITIONS.get(current_state, set())

    if new_state not in allowed:
        raise InvalidStateTransition(
            f"Cannot transition workload '{workload_id}' "
            f"from {current_state} → {new_state}. "
            f"Allowed: {allowed or 'none (terminal state)'}"
        )

    # Update workload state
    workload.state = new_state
    if new_state == WorkloadState.FAILED:
        workload.error_message = message

    # Write audit event
    event = WorkloadEvent(
        workload_id=workload.id,
        state=new_state,
        trigger=trigger,
        message=message,
    )
    db.add(event)
    db.commit()

    logger.info(
        "Workload %s: %s → %s (%s: %s)",
        workload_id, current_state, new_state, trigger, message,
    )
