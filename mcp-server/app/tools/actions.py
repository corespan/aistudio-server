"""The write tools: start a benchmark run, launch Jupyter, delete a run's results or stop a notebook."""
from __future__ import annotations

import re
from urllib.parse import quote

import httpx
import structlog
from langchain_core.tools import tool

from app.config import settings
from app.services import platform_api, request_ctx

logger = structlog.get_logger()


class Done(str):
    """A write that reached aistudio and succeeded. Only these are deduplicated (app/guards.py):
    every other answer (a refusal, a missing argument, an API error) lets a retry run again."""


# What GET /api/v1/models/config adds about the model that is not run config.
_MODEL_INFO_KEYS = ("gated", "license", "license_url", "hf_repo")
# Run config this tool takes a value for (the keys the worker and manifest builder read).
_FILLABLE = ("gpu_count", "concurrency", "max_model_len", "dataset_path")


def _blank_fields(config: dict) -> list[str]:
    """Config fields the catalog left for a human to fill in."""
    return sorted(
        k for k, v in config.items()
        if v is None or (isinstance(v, str) and not v.strip())
    )


def _resolve_config(cfg_full: dict, **overrides) -> tuple[dict, list[str]]:
    """The model's catalog config (without its licence info) with the user's explicit overrides applied."""
    config = {k: v for k, v in cfg_full.items() if k not in _MODEL_INFO_KEYS}
    unsupported: list[str] = []
    for name, value in overrides.items():
        if value is None or value == "":
            continue
        if name in config:
            config[name] = value
        else:
            unsupported.append(name)
    return config, sorted(unsupported)


def _start_payload(model: str, node_ip: str, config: dict) -> dict:
    """The body for POST /api/v1/benchmarks/start, with the verified caller as its creator."""
    return {"model_name": model, "node_ips": [node_ip], "config": dict(config),
            "created_by": _caller_sub()}


def _caller_sub() -> str | None:
    who = request_ctx.caller.get()
    return who.sub if who else None


def _detail_of(r: httpx.Response) -> str:
    """Extract the error detail from a response."""
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    return str(detail)[:300]


async def _call(method: str, path: str, *, params: dict | None = None,
                json_body: dict | None = None) -> httpx.Response:
    base = settings.aistudio_api_url.rstrip("/")
    async with httpx.AsyncClient(timeout=30) as c:
        return await c.request(method, f"{base}{path}", params=params, json=json_body)


@tool
async def start_workload(
    model: str,
    node_ip: str = "",
    dataset_path: str = "",
    gpu_count: int | None = None,
    concurrency: int | None = None,
    max_model_len: int | None = None,
) -> str:
    """Start a benchmark run of catalog `model` on GPU `node_ip`; `dataset_path` (a ShareGPT JSON file already on the node) is required; optional overrides: gpu_count, concurrency, max_model_len. Returns the task id."""
    if "jupyter" in model.lower():
        return ("Jupyter is not a benchmark model. To launch a Jupyter notebook use "
                "start_jupyter(node_ip=...) instead - it needs only a target node.")

    if not node_ip:
        return (f"To start `{model}` I need a target node (its IP or hostname). "
                "Tell me the node and I'll start the run.")

    try:
        r = await _call("GET", "/api/v1/models/config", params={"model": model})
        r.raise_for_status()
        cfg_full = r.json()
    except Exception as exc:
        logger.warning("start_workload.config_fetch_failed", model=model, error=str(exc))
        return f"Couldn't fetch default config for {model!r} from aistudio ({exc}). Not starting a run."

    config, unsupported = _resolve_config(
        cfg_full, gpu_count=gpu_count, concurrency=concurrency,
        max_model_len=max_model_len, dataset_path=dataset_path)
    if unsupported:
        return (
            f"`{model}` doesn't expose {', '.join(unsupported)} as a configurable knob. "
            f"Its config is:\n{_cfg_lines(config)}\n"
            "Drop that override (or name one of the fields above) and I'll start the run."
        )

    # Refuse while any config field is blank.
    if _blank_fields(config):
        return _needs_value(model, node_ip, config)

    payload = _start_payload(model, node_ip, config)
    r = await _call("POST", "/api/v1/benchmarks/start", json_body=payload)
    if r.status_code >= 400:
        logger.warning("start_workload.refused", status=r.status_code, model=model, node_ip=node_ip)
        return f"aistudio refused to start `{model}` on {node_ip} ({r.status_code}): {_detail_of(r)}"
    task_id = r.json().get("task_id")
    logger.info("start_workload.started", task_id=task_id, model=model, node_ip=node_ip)
    return Done(_workload_started(task_id, model, node_ip, config, gated=bool(cfg_full.get("gated"))))


@tool
async def start_jupyter(node_ip: str = "") -> str:
    """Launch a Jupyter Lab notebook on GPU node `node_ip`; returns the task id (jup-...)."""
    if not node_ip:
        return "To launch Jupyter I need a target node (its IP or hostname). Tell me the node."
    r = await _call("POST", "/api/v1/jupyter/launch",
                    json_body={"node_ip": node_ip, "created_by": _caller_sub()})
    if r.status_code >= 400:
        logger.warning("start_jupyter.refused", status=r.status_code, node_ip=node_ip)
        return f"aistudio refused to launch Jupyter on {node_ip} ({r.status_code}): {_detail_of(r)}"
    task_id = r.json().get("task_id")
    logger.info("start_jupyter.started", task_id=task_id, node_ip=node_ip)
    return Done(f"Jupyter Lab is launching on {node_ip}.\n  task_id: {task_id}\n"
                "Check progress with get_task_status; the notebook URL appears once it is READY.")


async def _creator_refusal(task_id: str) -> str | None:
    """None if the caller may stop `task_id` (its creator, or an admin); else the refusal.

    The creator is the workload's created_by in aistudio-server's database, which this server set
    from the verified JWT when it started the task."""
    who = request_ctx.caller.get()
    if who is None:
        return "Refused: no verified caller."
    if who.role == "admin":
        return None
    try:
        workload = await platform_api.workload(task_id)
    except Exception as exc:   # API error or unreachable: fail closed
        return (f"Refused: could not read who started {task_id} ({type(exc).__name__}: {exc}), "
                "so only an admin can stop it now.")
    creator = (workload or {}).get("created_by")
    if creator and creator == who.sub:
        return None
    whose = (f"it was started by {creator}" if creator else
             "aistudio has no creator on record for it (started before creators were recorded, "
             "or through the UI or REST API without one)")
    return (f"Refused: only the user who started {task_id} or an aistudio admin can stop or delete it, "
            f"and {whose}. Ask an admin to do it.")


# formatters

def _cfg_lines(config: dict) -> str:
    return "\n".join(f"    {k}: {v}" for k, v in config.items())


# Valid task id characters.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _workload_started(task_id: str | None, model: str, node_ip: str, config: dict,
                      *, gated: bool = False) -> str:
    """Format the started-run summary."""
    lines = [f"STARTED benchmark `{model}` on {node_ip}.", f"  task_id: {task_id}",
             "  config:", _cfg_lines(config)]
    if gated:
        lines.append("  " + chr(9888) + chr(65039) + " gated model " + chr(8212) +
                     " the node needs HF_TOKEN in ~/.aistudio/env for it.")
    lines.append("Follow it with get_task_status or get_task_logs.")
    return chr(10).join(lines)


def _needs_value(model: str, node_ip: str, config: dict) -> str:
    """Format the blank-fields refusal."""
    blank = _blank_fields(config)
    stuck = [f for f in blank if f not in _FILLABLE]
    if stuck:
        return (
            f"Can't start `{model}` on {node_ip} from here: its catalog entry leaves "
            f"{', '.join(stuck)} blank, and this tool has no parameter for "
            f"{'them' if len(stuck) > 1 else 'it'}. Do NOT ask the user to supply "
            f"{'these' if len(stuck) > 1 else 'this'} - say the model's catalog entry "
            "(catalog.json) has to be completed first, then the run can be started.\n"
            f"Current config:\n{_cfg_lines(config)}"
        )
    n = len(blank)
    return (
        f"Can't start this run yet: `{model}` on {node_ip} needs "
        f"{'a value for ' if n == 1 else f'values for all {n} of these: '}"
        f"{', '.join(blank)}. Current config:\n{_cfg_lines(config)}\n"
        f"Ask the user for {'that value' if n == 1 else 'every one of those ' + str(n) + ' values'}"
        " - for dataset_path it's an absolute path to a ShareGPT-format JSON file "
        "that already exists on the node. Don't guess any of them."
    )


@tool
async def stop_workload(task_id: str) -> str:
    """For a jup-... id, stop that Jupyter notebook and remove it; for a benchmark run id (wl-YYYYMMDD-xxxxxx), permanently delete its recorded results (a run in flight is not cancelled). Only its creator or an admin may; only for an id the user approved."""
    task_id = task_id.strip()
    if not task_id:
        return ("I need the exact id: wl-YYYYMMDD-xxxxxx for a benchmark run or jup-YYYYMMDD-xxxxxx "
                "for a notebook. Ask the user which one; do not guess one.")
    if not _TASK_ID_RE.fullmatch(task_id):
        return (f"{task_id!r} is not a task id (letters, digits, '.', '_' and '-' only). "
                "Ask the user for the exact id.")
    refusal = await _creator_refusal(task_id)
    if refusal:
        logger.warning("stop_workload.not_creator", task_id=task_id)
        return refusal

    if task_id.startswith("jup-"):
        r = await _call("DELETE", f"/api/v1/jupyter/instances/{quote(task_id, safe='')}")
        if r.status_code >= 400:
            logger.warning("stop_workload.refused", status=r.status_code, task_id=task_id)
            return f"aistudio refused to stop notebook {task_id} ({r.status_code}): {_detail_of(r)}"
        logger.info("stop_workload.stopped_jupyter", task_id=task_id)
        return Done(f"Stopped Jupyter notebook {task_id} and removed its record.")

    r = await _call("DELETE", f"/api/v1/benchmarks/{quote(task_id, safe='')}")
    if r.status_code >= 400:
        logger.warning("stop_workload.refused", status=r.status_code, task_id=task_id)
        return f"aistudio refused to delete run {task_id} ({r.status_code}): {_detail_of(r)}"
    logger.info("stop_workload.deleted", task_id=task_id)
    return Done(f"Deleted run {task_id}'s recorded results ({r.json().get('rows_deleted', '?')} row(s)). "
                "This cannot be undone. Any throughput_compare baseline that relied on this "
                "run is lost with it, and nothing was cancelled: a run still in flight keeps going.")
