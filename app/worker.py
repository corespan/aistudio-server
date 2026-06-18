import json
import logging
import re
from datetime import datetime, timezone

from celery import Celery, chain
from celery.exceptions import SoftTimeLimitExceeded

from app.config import settings, get_celery_broker_url, get_celery_result_backend
from app.database import SyncSessionLocal
from app.models.workload import Workload, WorkloadState
from app.models.node import Node
from app.models.task import Task
from app.models.task_log import TaskLog
from app.models.benchmark_result import BenchmarkResult
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


def _extract_gpu_type(specs: dict) -> str:
    """Return a short lowercase GPU family name, e.g. 'p40', 'a100', 't4'."""
    gpus = (specs or {}).get("gpus", [])
    if not gpus:
        return "unknown"
    name = gpus[0].get("name", "")
    m = re.search(r'\b([A-Za-z]*\d+[A-Za-z0-9]*)\b', name)
    return m.group(1).lower() if m else name.lower().split()[-1] if name else "unknown"


def _write_log(db, task_id, line: str) -> None:
    """Write a single log line to TaskLog and commit immediately so the SSE stream picks it up."""
    db.add(TaskLog(task_id=task_id, line=line))
    db.commit()


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

            # Create a Task so validate logs are visible in the SSE stream
            val_task = Task(
                workload_id=workload.id,
                node_id=nodes[0].id if nodes else None,
                run_name="%s-validate" % workload_id,
                task_config={},
                status="running",
            )
            db.add(val_task)
            db.commit()

            _write_log(db, val_task.id, "=== [1/3] Validating Node ===")

            for node in nodes:
                node.state = "VALIDATING"
                db.commit()
                _write_log(db, val_task.id, "Connecting to %s as %s..." % (node.machine_ip, node.machine_username))
                try:
                    specs = NodeInspector.inspect(node.machine_ip, node.machine_username)
                except Exception as e:
                    _write_log(db, val_task.id, "ERROR: %s" % e)
                    raise

                gpus = specs.get("gpus", [])
                if gpus:
                    for g in gpus:
                        mem_gb = round(g.get("memory_mb", 0) / 1024, 1)
                        _write_log(db, val_task.id, "GPU %d: %s  (%s GB VRAM)" % (g["index"], g["name"], mem_gb))
                else:
                    _write_log(db, val_task.id, "WARNING: No GPUs detected via nvidia-smi")

                _write_log(db, val_task.id, "Driver: %s   CUDA: %s" % (
                    specs.get("driver_version", "unknown"),
                    specs.get("cuda_version", "unknown"),
                ))
                node.specs = specs
                node.gpus = specs.get("gpus")
                node.state = "VALIDATED"
                db.commit()
                _write_log(db, val_task.id, "✓ Node %s validated." % node.machine_ip)

            val_task.status = "success"
            val_task.completed_at = datetime.now(timezone.utc)
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
                _write_log(db, install_task.id, "=== [2/3] Installing Dependencies ===")
                _write_log(db, install_task.id, "Node: %s" % node.machine_ip)
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
                workload.model_name, workload.workload_config,
            )
            cfg = workload.workload_config or {}
            _write_log(db, task.id, "=== [3/3] Running Benchmark ===")
            _write_log(db, task.id, "Model:       %s" % workload.model_name)
            _write_log(db, task.id, "Node:        %s" % node.machine_ip)
            _write_log(db, task.id, "Precision:   %s" % cfg.get("precision", "fp32"))
            _write_log(db, task.id, "Concurrency: %d   ISL: %d   OSL: %d" % (
                cfg.get("concurrency", 1),
                cfg.get("input_tokens", 0),
                cfg.get("output_tokens", 0),
            ))
            bench_started_at = datetime.now(timezone.utc)
            with SSHExecutor(node.machine_ip, node.machine_username, key_filename=settings.SSH_KEY_PATH) as ssh:
                exit_code = ssh.run_command(
                    "%s && %s" % (server_cmd, client_cmd), task.id,
                )
            bench_completed_at = datetime.now(timezone.utc)

            if exit_code != 0:
                task.status = "failed"
                task.completed_at = bench_completed_at
                node.state = "READY"
                node.running_task = None
                db.commit()
                raise RuntimeError("Benchmark exited with code %d" % exit_code)

            task.status = "success"
            task.completed_at = bench_completed_at
            node.state = "READY"
            node.running_task = None
            db.commit()

            # ── Parse BENCH_RESULT: line from task logs and store as BenchmarkResult
            result_log = (
                db.query(TaskLog)
                .filter(TaskLog.task_id == task.id, TaskLog.line.like("BENCH_RESULT:%"))
                .first()
            )
            if result_log:
                try:
                    metrics = json.loads(result_log.line[len("BENCH_RESULT:"):])
                    gpus_list = (node.specs or {}).get("gpus", [])
                    gpu_count = len(gpus_list) or 1
                    gpu_model = gpus_list[0].get("name", "") if gpus_list else ""
                    gpu_type = _extract_gpu_type(node.specs)
                    precision = cfg.get("precision", "fp32").lower()
                    db.add(BenchmarkResult(
                        run_id=workload_id,
                        sub_run_index=0,
                        model_name=workload.model_name.lower(),
                        pipeline_version="vllm-openai-latest",
                        node_ips=[node.machine_ip],
                        gpu_type=gpu_type,
                        gpu_count=gpu_count,
                        gpu_model=gpu_model,
                        precision=precision,
                        input_tokens=cfg.get("input_tokens", 0),
                        output_tokens=cfg.get("output_tokens", 0),
                        concurrency=cfg.get("concurrency", 1),
                        status="success",
                        total_token_throughput=metrics.get("total_token_throughput"),
                        mean_ttft_ms=metrics.get("mean_ttft_ms"),
                        mean_tpot_ms=metrics.get("mean_tpot_ms"),
                        mean_e2el_ms=metrics.get("mean_e2el_ms"),
                        metrics=metrics,
                        started_at=bench_started_at,
                        completed_at=bench_completed_at,
                        duration_seconds=(bench_completed_at - bench_started_at).total_seconds(),
                    ))
                    db.commit()
                    logger.info("BenchmarkResult saved for workload %s", workload_id)

                    # Write a human-readable results summary to the log stream
                    divider = "─" * 42
                    _write_log(db, task.id, divider)
                    _write_log(db, task.id, "Throughput:    %.1f tok/s" % (metrics.get("total_token_throughput") or 0))
                    if metrics.get("mean_ttft_ms"):
                        _write_log(db, task.id, "TTFT (mean):   %.0f ms" % metrics["mean_ttft_ms"])
                    if metrics.get("mean_tpot_ms"):
                        _write_log(db, task.id, "TPOT (mean):   %.1f ms" % metrics["mean_tpot_ms"])
                    _write_log(db, task.id, "E2E  (mean):   %.0f ms" % (metrics.get("mean_e2el_ms") or 0))
                    _write_log(db, task.id, "E2E  (p50):    %.0f ms" % (metrics.get("p50_e2el_ms") or 0))
                    _write_log(db, task.id, "E2E  (p99):    %.0f ms" % (metrics.get("p99_e2el_ms") or 0))
                    _write_log(db, task.id, "Requests:      %d / %d ok" % (
                        metrics.get("successful_requests", 0),
                        metrics.get("total_requests", 0),
                    ))
                    _write_log(db, task.id, "Duration:      %.1f s" % (metrics.get("duration_s") or 0))
                    _write_log(db, task.id, divider)
                    _write_log(db, task.id, "✓ Result saved to leaderboard.")
                except Exception as exc:
                    logger.warning("Could not save BenchmarkResult: %s", exc)

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
