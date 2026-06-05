import logging
from datetime import datetime, timezone

from celery import Celery, chain
from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings, get_celery_broker_url, get_celery_result_backend
from app.database import SyncSessionLocal
from app.models.workload import Workload, WorkloadState
from app.models.node import Node
from app.models.task import Task
from app.services.node_inspector import NodeInspector
from app.services.dependency_installer import DependencyInstaller
from app.services.manifest_builder import ManifestBuilder
from app.services.ssh_executor import SSHExecutor
from app.services.state_machine import transition_workload_state

logger = logging.getLogger(__name__)

# Celery App Configuration
celery_app = Celery(
    "aistudio_worker",
    broker=get_celery_broker_url(),
    backend=get_celery_result_backend(),
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=1500,
    task_time_limit=1800,
)


def _fail_workload(workload_id, trigger, error):
    """Transition a workload to FAILED with an error message."""
    try:
        with SyncSessionLocal() as db:
            transition_workload_state(
                db, workload_id, WorkloadState.FAILED,
                trigger=trigger, message=error,
            )
    except Exception:
        logger.critical(
            "WORKLOAD %s STUCK: could not write FAILED state (trigger=%s). "
            "Manual DB intervention required.",
            workload_id, trigger,
            exc_info=True,
        )


@celery_app.task(bind=True, soft_time_limit=600, time_limit=660)
def validate_node(self, workload_id):
    """SSH into the nodes, verify GPU specs, write to DB."""
    try:
        with SyncSessionLocal() as db:
            transition_workload_state(
                db, workload_id, WorkloadState.VALIDATING,
                trigger="validate_node",
                message="Starting node validation",
            )
            workload = db.query(Workload).filter(Workload.workload_id == workload_id).first()
            nodes = db.query(Node).filter(Node.workload_id == workload.id).all()
            for node in nodes:
                node.state = "VALIDATING"
                db.commit()
                specs = NodeInspector.inspect(node.machine_ip, node.machine_username)
                node.specs = specs
                node.gpus = specs.get("gpus")
                node.state = "VALIDATED"
                db.commit()
            transition_workload_state(
                db, workload_id, WorkloadState.VALIDATED,
                trigger="validate_node",
                message="All %d node(s) passed validation" % len(nodes),
            )
    except SoftTimeLimitExceeded:
        _fail_workload(workload_id, "validate_node", "Node validation timed out")
        raise
    except Exception as exc:
        _fail_workload(workload_id, "validate_node", str(exc))
        raise
    return workload_id


@celery_app.task(bind=True, soft_time_limit=900, time_limit=960)
def install_dependencies(self, workload_id):
    """SSH into nodes and install required software (vLLM/Docker)."""
    try:
        with SyncSessionLocal() as db:
            transition_workload_state(
                db, workload_id, WorkloadState.INSTALLING,
                trigger="install_dependencies",
                message="Installing dependencies on nodes",
            )
            workload = db.query(Workload).filter(Workload.workload_id == workload_id).first()
            nodes = db.query(Node).filter(Node.workload_id == workload.id).all()
            for node in nodes:
                node.state = "INSTALLING"
                install_task = Task(
                    workload_id=workload.id,
                    node_id=node.id,
                    run_name="%s-%s-install" % (workload_id, node.machine_id),
                    task_config={},
                    status="running",
                )
                db.add(install_task)
                db.commit()
                success = DependencyInstaller.install_vllm(
                    node.machine_ip, node.machine_username, install_task.id,
                )
                if success:
                    install_task.status = "success"
                    install_task.completed_at = datetime.now(timezone.utc)
                    node.sw_installed = True
                    node.state = "READY"
                else:
                    install_task.status = "failed"
                    install_task.completed_at = datetime.now(timezone.utc)
                    node.state = "FAILED"
                    db.commit()
                    raise RuntimeError(
                        "Dependency installation failed on node %s" % node.machine_ip
                    )
                db.commit()
            transition_workload_state(
                db, workload_id, WorkloadState.READY,
                trigger="install_dependencies",
                message="All dependencies installed successfully",
            )
    except SoftTimeLimitExceeded:
        _fail_workload(workload_id, "install_dependencies", "Installation timed out")
        raise
    except Exception as exc:
        _fail_workload(workload_id, "install_dependencies", str(exc))
        raise
    return workload_id


@celery_app.task(bind=True, soft_time_limit=1500, time_limit=1800)
def execute_benchmark(self, workload_id):
    """Orchestrate the benchmark run on the first node."""
    try:
        with SyncSessionLocal() as db:
            transition_workload_state(
                db, workload_id, WorkloadState.RUNNING,
                trigger="execute_benchmark",
                message="Starting benchmark execution",
            )
            workload = db.query(Workload).filter(Workload.workload_id == workload_id).first()
            nodes = db.query(Node).filter(Node.workload_id == workload.id).all()
            if not nodes:
                raise RuntimeError("No nodes available for benchmark execution")
            node = nodes[0]
            node.state = "RUNNING"
            node.running_task = workload_id
            db.commit()
            task = Task(
                workload_id=workload.id,
                node_id=node.id,
                run_name=workload_id,
                task_config=workload.workload_config,
                status="running",
            )
            db.add(task)
            db.commit()
            server_cmd = ManifestBuilder.build_vllm_command(
                workload.model_name, workload.workload_config,
            )
            client_cmd = ManifestBuilder.build_benchmark_client_command(
                workload.workload_config,
            )
            with SSHExecutor(node.machine_ip, node.machine_username, key_filename=settings.SSH_KEY_PATH) as ssh:
                exit_code = ssh.run_command(
                    "%s && %s" % (server_cmd, client_cmd), task.id,
                )
            if exit_code != 0:
                task.status = "failed"
                task.completed_at = datetime.now(timezone.utc)
                node.state = "READY"
                node.running_task = None
                db.commit()
                raise RuntimeError("Benchmark exited with code %d" % exit_code)
            task.status = "success"
            task.completed_at = datetime.now(timezone.utc)
            node.state = "READY"
            node.running_task = None
            db.commit()
            transition_workload_state(
                db, workload_id, WorkloadState.READY,
                trigger="execute_benchmark",
                message="Benchmark completed successfully",
            )
    except SoftTimeLimitExceeded:
        _fail_workload(workload_id, "execute_benchmark", "Benchmark execution timed out")
        raise
    except Exception as exc:
        _fail_workload(workload_id, "execute_benchmark", str(exc))
        raise
    return workload_id


@celery_app.task
def start_benchmark_chain(workload_id):
    """
    Entrypoint called by FastAPI. Assembles the Celery chain.
    The workload is already in CREATED state (set by the router).
    Each task in the chain returns workload_id so the next task receives it.
    """
    workflow = chain(
        validate_node.s(workload_id),
        install_dependencies.s(),
        execute_benchmark.s(),
    )
    workflow.apply_async()
