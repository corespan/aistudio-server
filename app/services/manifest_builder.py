import base64
import textwrap
from typing import Any, Dict

from app.config import settings, get_workload_registry

# GCR login command — shared by vLLM/Jupyter server commands and DependencyInstaller.
# Defined once here so it's never out of sync across callers.
_GCR_LOGIN_CMD = (
    "cat $HOME/gcr.json | docker login -u _json_key "
    "--password-stdin https://us-docker.pkg.dev"
)


# ── Result parser (runs inside the vLLM container via base64 exec) ───────────
# After `vllm bench serve --save-result` writes /tmp/bench_result.json,
# this script reads it and prints the single "BENCH_RESULT:{json}" line that
# execute_benchmark() parses to create a BenchmarkResult row.
#
# vllm bench serve produces all the TTFT/TPOT/E2EL metrics via the official
# streaming benchmark tool — far more accurate than our old urllib script.
_PARSE_SCRIPT = textwrap.dedent("""\
    import json, sys
    try:
        with open('/tmp/bench_result.json') as f:
            content = f.read().strip()
        # vllm bench --save-result writes a single JSON object (no --append-result).
        # Guard against JSONL in case --append-result was used previously.
        try:
            d = json.loads(content)
        except json.JSONDecodeError:
            lines = [l for l in content.split('\\n') if l.strip()]
            d = json.loads(lines[-1]) if lines else {}

        def _r(v, digits=2):
            return round(v, digits) if v is not None else None

        res = {
            "total_requests":          d.get("num_prompts", 0),
            "successful_requests":     d.get("completed", 0),
            "duration_s":              _r(d.get("duration"), 3),
            "total_token_throughput":  _r(d.get("total_token_throughput")),
            "output_token_throughput": _r(d.get("output_throughput")),
            "mean_ttft_ms":            _r(d.get("mean_ttft_ms")),
            "p99_ttft_ms":             _r(d.get("p99_ttft_ms")),
            "mean_tpot_ms":            _r(d.get("mean_tpot_ms")),
            "mean_e2el_ms":            _r(d.get("mean_e2el_ms")),
            "p50_e2el_ms":             _r(d.get("p50_e2el_ms")),
            "p99_e2el_ms":             _r(d.get("p99_e2el_ms")),
        }
        tpt = res["total_token_throughput"] or 0
        e2e = res["mean_e2el_ms"] or 0
        ttft = res["mean_ttft_ms"] or 0
        print(
            "Done: %d/%d ok  throughput=%.1f tok/s  ttft=%.0f ms  e2el=%.0f ms"
            % (res["successful_requests"], res["total_requests"], tpt, ttft, e2e),
            flush=True,
        )
        print("BENCH_RESULT:" + json.dumps(res), flush=True)
    except Exception as e:
        print("ERROR parsing bench_result.json: " + str(e), file=sys.stderr, flush=True)
        sys.exit(1)
""")

# Encode once at import time; safe to embed in any docker exec command.
_PARSE_B64 = base64.b64encode(_PARSE_SCRIPT.encode()).decode()

# Map UI precision labels → vLLM --dtype values
_DTYPE_MAP = {
    "fp32": "float32",   "float32":  "float32",
    "fp16": "float16",   "float16":  "float16",
    "bf16": "bfloat16",  "bfloat16": "bfloat16",
    "fp8":  "float8_e4m3fn",
}


class ManifestBuilder:
    """
    Builds the shell command strings executed on GPU nodes via SSHExecutor.

    server_cmd  – starts the vLLM OpenAI-compatible server as a detached container
    client_cmd  – waits for the server, runs the benchmark, prints BENCH_RESULT:
    """

    @staticmethod
    def build_vllm_command(model_name: str, config: Dict[str, Any],
                           image_tag: str = None, run_id: str = "") -> str:
        gpu_count     = config.get("gpu_count", 1)
        precision     = config.get("precision", "fp32")
        max_model_len = config.get("max_model_len")   # None → let vLLM read from model config
        dtype         = _DTYPE_MAP.get(precision.lower(), "float32")

        tag   = image_tag or settings.WORKLOAD_IMAGE_TAG
        image = "%s/llminference:%s" % (get_workload_registry(), tag)

        # Container name embeds the run_id so each benchmark run is identifiable
        # on the node via `docker ps` or `docker logs`.
        container_name = "vllm-%s" % run_id if run_id else "vllm_server"

        # Volume mount depends on where model weights live (set via MODEL_STORAGE_MODE in .env)
        mode = settings.MODEL_STORAGE_MODE
        if mode == "local":
            volume_flag = "-v %s:/models" % settings.MODEL_LOCAL_PATH
            env_flags   = "-e HF_HOME=/models"
        elif mode == "gcs":
            volume_flag = ""
            env_flags   = "-e GCS_BUCKET=%s" % settings.MODEL_GCS_BUCKET
        else:  # huggingface (default)
            volume_flag = "-v $HOME/.cache/huggingface:/root/.cache/huggingface"
            env_flags   = ""

        login_cmd = _GCR_LOGIN_CMD
        # Remove only the named container from a previous run — no need to touch
        # other containers since we use a dynamically assigned free port below.
        rm_cmd = "docker rm -f %s 2>/dev/null || true" % container_name
        # Port is resolved at runtime via $VLLM_PORT (set in worker before this command
        # is called). Using a free port means no conflict regardless of what else is
        # running on the node. The container's internal port is always 8000; the host
        # port is whatever $VLLM_PORT resolves to.
        # No --rm: container stays in stopped state after the benchmark so you can
        # inspect it with `docker ps -a` and `docker logs <name>`.
        # The rm_cmd at the start of the next run cleans it up.
        run_cmd   = " ".join(p for p in [
            "docker run --gpus all -d --name %s" % container_name,
            "-p $VLLM_PORT:8000 --ipc=host",
            "--entrypoint python3",
            volume_flag,
            env_flags,
            image,
            "-m vllm.entrypoints.openai.api_server",
            "--model %s" % model_name,
            "--dtype %s" % dtype,
            # Only pass --max-model-len when explicitly set in config.
            # If omitted, vLLM auto-reads max_position_embeddings from the
            # model's config.json — this prevents startup failures when the
            # user-specified value exceeds the model's physical limit.
            ("--max-model-len %d" % max_model_len if max_model_len else ""),
            "--tensor-parallel-size %d" % gpu_count,
        ] if p)
        return "%s && %s && %s" % (login_cmd, rm_cmd, run_cmd)

    @staticmethod
    def build_benchmark_client_command(model_name: str, config: Dict[str, Any],
                                       run_id: str = "") -> str:
        concurrency   = config.get("concurrency", 4)
        input_tokens  = config.get("input_tokens", 512)
        output_tokens = config.get("output_tokens", 128)
        num_requests  = max(concurrency * 5, 20)

        # Must match the container name used in build_vllm_command.
        container_name = "vllm-%s" % run_id if run_id else "vllm_server"

        # ── 1. Wait for the vLLM server to become healthy (up to 10 min) ──────
        # Uses $VLLM_PORT which is set by the worker before this command runs.
        wait = (
            "echo \"Waiting for vLLM server on port $VLLM_PORT (up to 10 min)...\" && "
            "READY=0 && "
            "for i in $(seq 1 600); do "
            "  curl -sf http://localhost:$VLLM_PORT/health >/dev/null 2>&1 && READY=1 && break; "
            "  [ $((i %% 10)) -eq 0 ] && echo \"[$i/600] not ready yet...\"; sleep 1; "
            "done && "
            "[ \"$READY\" -eq 1 ] || "
            "{ echo \"ERROR: vLLM server did not start in 10 min\"; "
            "  docker stop %(name)s 2>/dev/null; exit 1; } && "
            "echo \"vLLM server ready on port $VLLM_PORT.\""
        ) % {"name": container_name}

        # ── 2. Run `vllm bench serve` inside the container ───────────────────
        # The vLLM container already has the vllm package installed, so we
        # docker-exec into it to use the official streaming benchmark tool.
        # This measures TTFT, TPOT, and E2EL properly — the old urllib script
        # could not measure TTFT because it used non-streaming completions.
        #
        # The server inside the container listens on port 8000 (internal).
        # `vllm bench serve` with --dataset-name random generates prompts of
        # exactly --random-input-len tokens, so ISL/OSL are precisely controlled.
        bench = (
            "echo \"Running benchmark: model=%(model)s concurrency=%(conc)d "
            "isl=%(isl)d osl=%(osl)d n=%(n)d\" && "
            "docker exec %(name)s vllm bench serve "
            "--backend vllm "
            "--host 0.0.0.0 "
            "--port 8000 "
            "--endpoint /v1/completions "
            "--model %(model)s "
            "--dataset-name random "
            "--random-input-len %(isl)d "
            "--random-output-len %(osl)d "
            "--num-prompts %(n)d "
            "--max-concurrency %(conc)d "
            "--ignore-eos "
            "--percentile-metrics ttft,tpot,itl,e2el "
            "--save-result "
            "--result-dir /tmp "
            "--result-filename bench_result.json "
            "--trust-remote-code"
        ) % {
            "name":  container_name,
            "model": model_name,
            "isl":   input_tokens,
            "osl":   output_tokens,
            "n":     num_requests,
            "conc":  concurrency,
        }

        # ── 3. Parse /tmp/bench_result.json inside the container and print BENCH_RESULT:
        parse = (
            "docker exec %(name)s python3 -c "
            "\"import base64; exec(base64.b64decode('%(b64)s').decode())\""
        ) % {"name": container_name, "b64": _PARSE_B64}

        # ── 4. Always stop the vLLM container whether benchmark passes or fails
        cleanup = "docker stop %s 2>/dev/null || true" % container_name

        return "%s && %s && %s ; %s" % (wait, bench, parse, cleanup)

    @staticmethod
    def build_jupyter_command(run_id: str = "") -> str:
        """
        Start the jupyternotebook GCR image on the node.

        Runs the container's CMD (script.sh → startJupyter.py) directly — no
        entrypoint override. script.sh copies notebooks to NFS at /data/{workload_id}/
        and startJupyter.py launches Jupyter from that directory so:
          - Only notebooks are visible (no Dockerfile/script.sh/etc.)
          - All user edits persist on NFS
        """
        image = "%s/jupyternotebook:%s" % (get_workload_registry(), settings.JUPYTER_IMAGE_TAG)
        container_name = "jupyter-%s" % run_id if run_id else "jupyter_server"

        login_cmd = _GCR_LOGIN_CMD
        rm_cmd    = "docker rm -f %s 2>/dev/null || true" % container_name
        # Pass workload_id and port as env vars so script.sh and startJupyter.py
        # write notebooks to the correct NFS path (/data/{workload_id}/).
        # $JUPYTER_PORT is resolved at runtime by the worker before this command runs.
        run_cmd   = " ".join([
            "docker run --gpus all -d --name %s" % container_name,
            "-p $JUPYTER_PORT:7008 --ipc=host",
            "-v /data:/data",
            "-e workload_id=%s" % run_id,
            "-e workload_port=7008",
            image,
        ])
        return "%s && %s && %s" % (login_cmd, rm_cmd, run_cmd)

    @staticmethod
    def build_jupyter_health_command(run_id: str = "") -> str:
        """Poll localhost:8899 until Jupyter is ready (up to 5 min)."""
        container_name = "jupyter-%s" % run_id if run_id else "jupyter_server"
        return (
            "READY=0 && "
            "for i in $(seq 1 300); do "
            "  curl -sf http://localhost:$JUPYTER_PORT/api >/dev/null 2>&1 && READY=1 && break; "
            "  [ $((i %% 10)) -eq 0 ] && echo \"[$i/300] waiting for Jupyter...\"; sleep 1; "
            "done && "
            "[ \"$READY\" -eq 1 ] || "
            "{ echo 'ERROR: Jupyter did not start in 5 min'; "
            "  docker stop %(name)s 2>/dev/null; exit 1; } && "
            "echo 'Jupyter ready.'"
        ) % {"name": container_name}
