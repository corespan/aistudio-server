import json
import logging
import re
from datetime import datetime, timezone

from celery import Celery, chain
from celery.exceptions import SoftTimeLimitExceeded

from sqlalchemy.orm.attributes import flag_modified

from app.config import settings, get_celery_broker_url, get_celery_result_backend
from app.database import SyncSessionLocal
from app.models.workload import Workload, WorkloadState
from app.models.node import Node
from app.models.task import Task
from app.models.task_log import TaskLog
from app.models.benchmark_result import BenchmarkResult
from app.models.workload_type import WorkloadType
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
    """
    Transition a workload to FAILED with an error message.

    For benchmark workloads (not Jupyter) also writes a BenchmarkResult row with
    status='failed' so the run appears in the leaderboard API after a page refresh
    instead of disappearing when the in-memory stream store is cleared.
    """
    try:
        with SyncSessionLocal() as db:
            transition_workload_state(
                db, workload_id, WorkloadState.FAILED,
                trigger=trigger, message=error,
            )
            # Write a minimal BenchmarkResult so GET /api/v1/benchmarks returns
            # the failed run. Skip Jupyter workloads — they have no benchmark metrics.
            workload = db.query(Workload).filter(Workload.workload_id == workload_id).first()
            if workload and (workload.workload_config or {}).get("workload_type") != "jupyter":
                existing = db.query(BenchmarkResult).filter(
                    BenchmarkResult.run_id == workload_id
                ).first()
                if not existing:
                    node = db.query(Node).filter(Node.workload_id == workload.id).first()
                    now = datetime.now(timezone.utc)
                    db.add(BenchmarkResult(
                        run_id=workload_id,
                        sub_run_index=0,
                        model_name=(workload.model_name or "").lower(),
                        pipeline_version="vllm-openai-latest",
                        node_ips=[node.machine_ip] if node else [],
                        gpu_type="",
                        gpu_count=0,
                        gpu_model="",
                        precision=(workload.workload_config or {}).get("precision", ""),
                        input_tokens=0,
                        output_tokens=0,
                        concurrency=0,
                        status="failed",
                        total_token_throughput=None,
                        mean_ttft_ms=None,
                        mean_tpot_ms=None,
                        mean_e2el_ms=None,
                        metrics={},
                        started_at=now,
                        completed_at=now,
                        duration_seconds=0,
                    ))
                    db.commit()
    except Exception:
        logger.critical(
            "WORKLOAD %s STUCK: could not write FAILED state (trigger=%s). "
            "Manual DB intervention required.",
            workload_id, trigger,
            exc_info=True,
        )


def _fetch_workload_and_nodes(db, workload_id: str):
    """Fetch the Workload record and its associated Nodes in one call."""
    workload = db.query(Workload).filter(Workload.workload_id == workload_id).first()
    nodes = db.query(Node).filter(Node.workload_id == workload.id).all()
    return workload, nodes


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
            workload, nodes = _fetch_workload_and_nodes(db, workload_id)

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
                _write_log(db, val_task.id, "Server:   %s" % specs.get("server_name", node.machine_ip))
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
            workload, nodes = _fetch_workload_and_nodes(db, workload_id)
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
            workload, nodes = _fetch_workload_and_nodes(db, workload_id)
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
            # Look up the per-workload image tag from workload_types table.
            # Falls back to settings.WORKLOAD_IMAGE_TAG if not seeded.
            wt = (
                db.query(WorkloadType)
                .filter(WorkloadType.name == "LLMInference")
                .first()
            )
            image_tag = wt.image_tag if wt and wt.image_tag else None
            server_cmd = ManifestBuilder.build_vllm_command(
                workload.model_name, workload.workload_config,
                image_tag=image_tag, run_id=workload_id,
            )
            client_cmd = ManifestBuilder.build_benchmark_client_command(
                workload.model_name, workload.workload_config, run_id=workload_id,
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
            # Pick a free host port at runtime so we never collide with anything
            # already running on the node. $VLLM_PORT is shared across server_cmd
            # and client_cmd since they run in the same SSH session.
            port_cmd = (
                "VLLM_PORT=$(python3 -c \""
                "import socket; s=socket.socket(); s.bind(('',0)); "
                "p=s.getsockname()[1]; s.close(); print(p)"
                "\") && echo \"Using port $VLLM_PORT for vLLM server\""
            )
            # Print GPU state before starting so we can confirm the GPU is real
            # and see it transition from idle to loaded during the health-check wait.
            gpu_cmd = (
                "echo '--- GPU state before benchmark ---' && "
                "nvidia-smi --query-gpu=name,memory.used,memory.free,utilization.gpu "
                "--format=csv,noheader,nounits 2>/dev/null | "
                "awk -F',' '{printf \"GPU: %s  VRAM used: %s MB  free: %s MB  util: %s%%\\n\",$1,$2,$3,$4}' "
                "|| echo 'nvidia-smi not available'"
            )
            bench_started_at = datetime.now(timezone.utc)
            with SSHExecutor(node.machine_ip, node.machine_username, key_filename=settings.SSH_KEY_PATH) as ssh:
                exit_code = ssh.run_command(
                    "%s && %s && %s && %s" % (port_cmd, gpu_cmd, server_cmd, client_cmd), task.id,
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
                    server_name = (node.specs or {}).get("server_name", node.machine_ip)
                    db.add(BenchmarkResult(
                        run_id=workload_id,
                        sub_run_index=0,
                        model_name=workload.model_name.lower(),
                        pipeline_version="vllm-openai-latest",
                        node_ips=[node.machine_ip],
                        gpu_type=gpu_type,
                        gpu_count=gpu_count,
                        gpu_model=gpu_model,
                        server_name=server_name,
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


@celery_app.task(bind=True, soft_time_limit=900, time_limit=960)
def launch_jupyter(self, workload_id):
    """Pull the llminference image and start a Jupyter Lab server on the node."""
    try:
        with SyncSessionLocal() as db:
            transition_workload_state(
                db, workload_id, WorkloadState.INSTALLING,
                trigger="launch_jupyter",
                message="Launching Jupyter Lab server",
            )
            workload, nodes = _fetch_workload_and_nodes(db, workload_id)
            if not nodes:
                raise RuntimeError("No nodes found for workload %s" % workload_id)
            node = nodes[0]
            node.state = "INSTALLING"

            task = Task(
                workload_id=workload.id,
                node_id=node.id,
                run_name="%s-jupyter" % workload_id,
                task_config={},
                status="running",
            )
            db.add(task)
            db.commit()

            _write_log(db, task.id, "=== [2/2] Launching Jupyter Lab ===")
            _write_log(db, task.id, "Node: %s" % node.machine_ip)

            # Discover a free host port at runtime — avoids 8899 being already in
            # use on the node. The port is exported as $JUPYTER_PORT and used by
            # both the docker run (-p $JUPYTER_PORT:7008) and the health-check curl.
            # We echo "JUPYTER_PORT=<n>" so the worker can parse it back from logs.
            port_cmd = (
                "JUPYTER_PORT=$(python3 -c \""
                "import socket; s=socket.socket(); s.bind(('',0)); "
                "p=s.getsockname()[1]; s.close(); print(p)"
                "\") && echo \"JUPYTER_PORT=$JUPYTER_PORT\""
            )
            server_cmd = ManifestBuilder.build_jupyter_command(run_id=workload_id)
            health_cmd = ManifestBuilder.build_jupyter_health_command(run_id=workload_id)

            with SSHExecutor(node.machine_ip, node.machine_username, key_filename=settings.SSH_KEY_PATH) as ssh:
                exit_code = ssh.run_command(
                    "%s && %s && %s" % (port_cmd, server_cmd, health_cmd), task.id,
                )

            if exit_code != 0:
                task.status = "failed"
                task.completed_at = datetime.now(timezone.utc)
                node.state = "FAILED"
                db.commit()
                raise RuntimeError("Jupyter launch failed with exit code %d" % exit_code)

            # Parse the port from the log line "JUPYTER_PORT=<n>" written above.
            jupyter_port = 7008  # fallback (container internal port)
            from app.models.task_log import TaskLog as _TaskLog
            port_logs = db.query(_TaskLog).filter(_TaskLog.task_id == task.id).all()
            for _log in port_logs:
                if _log.line.startswith("JUPYTER_PORT="):
                    try:
                        jupyter_port = int(_log.line.split("=", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
                    break

            jupyter_url = "http://%s:%d/lab" % (node.machine_ip, jupyter_port)
            _write_log(db, task.id, "✓ Jupyter Lab running at: %s" % jupyter_url)

            task.status = "success"
            task.completed_at = datetime.now(timezone.utc)
            node.state = "READY"
            db.commit()

            # Re-query workload fresh — after multiple db.commit() calls SQLAlchemy
            # expires the cached object, and JSONB mutations aren't tracked unless
            # we re-fetch and explicitly mark the column dirty.
            workload = db.query(Workload).filter(Workload.workload_id == workload_id).first()
            new_cfg = dict(workload.workload_config or {})
            new_cfg["jupyter_url"] = jupyter_url
            workload.workload_config = new_cfg
            flag_modified(workload, "workload_config")
            db.commit()

            transition_workload_state(
                db, workload_id, WorkloadState.READY,
                trigger="launch_jupyter",
                message="Jupyter Lab started at %s" % jupyter_url,
            )
    except SoftTimeLimitExceeded:
        _fail_workload(workload_id, "launch_jupyter", "Jupyter launch timed out")
        raise
    except Exception as exc:
        _fail_workload(workload_id, "launch_jupyter", str(exc))
        raise
    return workload_id


@celery_app.task
def start_jupyter_chain(workload_id):
    """Validate the node then launch Jupyter Lab."""
    workflow = chain(
        validate_node.s(workload_id),
        launch_jupyter.s(),
    )
    workflow.apply_async()
