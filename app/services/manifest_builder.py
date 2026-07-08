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


# ── Benchmark Python script (runs on the remote node via base64 exec) ────────
# Sends concurrent completions requests to the local vLLM server and collects
# E2E latency + throughput metrics. Outputs a single "BENCH_RESULT:{json}" line
# that execute_benchmark() parses to create a BenchmarkResult row.
_BENCHMARK_SCRIPT = textwrap.dedent("""\
    import os, time, threading, json, urllib.request

    model         = os.environ["BENCH_MODEL"]
    concurrency   = int(os.environ["BENCH_CONCURRENCY"])
    input_tokens  = int(os.environ["BENCH_INPUT_TOKENS"])
    output_tokens = int(os.environ["BENCH_OUTPUT_TOKENS"])
    num_requests  = int(os.environ["BENCH_NUM_REQUESTS"])

    url    = "http://localhost:8000/v1/completions"
    prompt = ("bench " * 2000)[: input_tokens * 5]

    results = []
    lock    = threading.Lock()

    def send_req(i):
        data = json.dumps({
            "model": model, "prompt": prompt,
            "max_tokens": output_tokens, "temperature": 0,
        }).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read())
            t1 = time.time()
            toks = body.get("usage", {}).get("completion_tokens", output_tokens)
            with lock:
                results.append({"ok": True, "latency": t1 - t0, "tokens": toks})
        except Exception as e:
            with lock:
                results.append({"ok": False, "error": str(e)})
            print("Request %d failed: %s" % (i, e), flush=True)

    print(
        "Benchmarking: model=%s concurrency=%d input=%d output=%d n=%d"
        % (model, concurrency, input_tokens, output_tokens, num_requests),
        flush=True,
    )
    t_start = time.time()
    pending = list(range(num_requests))
    active  = []
    while pending or active:
        active = [t for t in active if t.is_alive()]
        while len(active) < concurrency and pending:
            t = threading.Thread(target=send_req, args=(pending.pop(0),))
            t.start()
            active.append(t)
        if pending or active:
            time.sleep(0.05)
    duration = time.time() - t_start

    ok        = [r for r in results if r.get("ok")]
    latencies = sorted(r["latency"] for r in ok)
    total_out = sum(r["tokens"] for r in ok)
    total_tok = (input_tokens + output_tokens) * len(ok)
    mean_lat  = sum(latencies) / len(latencies) if latencies else None
    p50       = latencies[len(latencies) // 2]                  if latencies else None
    p99       = latencies[int(len(latencies) * 0.99)]           if latencies else None

    res = {
        "total_requests":        num_requests,
        "successful_requests":   len(ok),
        "duration_s":            round(duration, 3),
        "total_token_throughput":  round(total_tok / duration, 2) if duration else 0,
        "output_token_throughput": round(total_out / duration, 2) if duration else 0,
        "mean_e2el_ms": round(mean_lat * 1000, 2)                            if mean_lat else None,
        "p50_e2el_ms":  round(p50 * 1000, 2)                                 if p50      else None,
        "p99_e2el_ms":  round(p99 * 1000, 2)                                 if p99      else None,
        "mean_ttft_ms": None,
        "mean_tpot_ms": round(mean_lat * 1000 / output_tokens, 2)
                        if mean_lat and output_tokens else None,
    }
    print(
        "Done: %d/%d ok  throughput=%.1f tok/s  e2el=%.0f ms"
        % (len(ok), num_requests, res["total_token_throughput"], res["mean_e2el_ms"] or 0),
        flush=True,
    )
    print("BENCH_RESULT:" + json.dumps(res), flush=True)
""")

# Encode once at import time; safe to embed in any shell command.
_SCRIPT_B64 = base64.b64encode(_BENCHMARK_SCRIPT.encode()).decode()

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
        max_model_len = config.get("max_model_len", 2048)
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
        rm_cmd    = "docker rm -f %s 2>/dev/null || true" % container_name
        # Override the image's default CMD (script.sh installs aiAgent from Nexus).
        # The image already has vllm==0.14.1; --entrypoint bypasses the orchestration wrapper.
        run_cmd   = " ".join(p for p in [
            "docker run --gpus all --rm -d --name %s" % container_name,
            "-p 8000:8000 --ipc=host",
            "--entrypoint python3",
            volume_flag,
            env_flags,
            image,
            "-m vllm.entrypoints.openai.api_server",
            "--model %s" % model_name,
            "--dtype %s" % dtype,
            "--max-model-len %d" % max_model_len,
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
        wait = (
            "echo 'Waiting for vLLM server (up to 10 min)...' && "
            "READY=0 && "
            "for i in $(seq 1 120); do "
            "  curl -sf http://localhost:8000/health >/dev/null 2>&1 && READY=1 && break; "
            "  echo \"[$i/120] not ready, waiting 5s...\"; sleep 5; "
            "done && "
            "[ \"$READY\" -eq 1 ] || "
            "{ echo 'ERROR: vLLM server did not start in 10 min'; "
            "  docker stop %(name)s 2>/dev/null; exit 1; } && "
            "echo 'vLLM server ready.'"
        ) % {"name": container_name}

        # ── 2. Export config as env vars and run the embedded benchmark ───────
        bench = (
            "export BENCH_MODEL='%(model)s' && "
            "export BENCH_CONCURRENCY=%(concurrency)d && "
            "export BENCH_INPUT_TOKENS=%(input_tokens)d && "
            "export BENCH_OUTPUT_TOKENS=%(output_tokens)d && "
            "export BENCH_NUM_REQUESTS=%(num_requests)d && "
            "python3 -c \"import base64; exec(base64.b64decode('%(b64)s').decode())\""
        ) % {
            "model":        model_name,
            "concurrency":  concurrency,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "num_requests": num_requests,
            "b64":          _SCRIPT_B64,
        }

        # ── 3. Always stop the vLLM container whether benchmark passes or fails
        cleanup = "docker stop %s 2>/dev/null || true" % container_name

        return "%s && %s ; %s" % (wait, bench, cleanup)

    @staticmethod
    def build_jupyter_command(run_id: str = "") -> str:
        """
        Start the jupyternotebook GCR image on host port 8899 (container port 7008).

        The image's default CMD runs script.sh which needs Nexus + aiAgent.
        We bypass that entirely with --entrypoint bash and run jupyter notebook directly.
        The image already has notebook==7.4.7 installed via requirements.txt.
        """
        image = "%s/jupyternotebook:%s" % (get_workload_registry(), settings.JUPYTER_IMAGE_TAG)
        container_name = "jupyter-%s" % run_id if run_id else "jupyter_server"

        # Inner command run inside the container via bash -c.
        # Base64-encoded to avoid any shell quoting issues on the remote host.
        inner = (
            "jupyter notebook --no-browser --ip=0.0.0.0 --port=7008 "
            "--NotebookApp.token= --NotebookApp.password= --allow-root"
        )
        inner_b64 = base64.b64encode(inner.encode()).decode()

        login_cmd = _GCR_LOGIN_CMD
        rm_cmd    = "docker rm -f %s 2>/dev/null || true" % container_name
        run_cmd   = " ".join([
            "docker run --gpus all -d --name %s" % container_name,
            "-p 8899:7008 --ipc=host",
            "--entrypoint bash",
            image,
            '-c "$(echo %s | base64 -d)"' % inner_b64,
        ])
        return "%s && %s && %s" % (login_cmd, rm_cmd, run_cmd)

    @staticmethod
    def build_jupyter_health_command(run_id: str = "") -> str:
        """Poll localhost:8899 until Jupyter is ready (up to 5 min)."""
        container_name = "jupyter-%s" % run_id if run_id else "jupyter_server"
        return (
            "READY=0 && "
            "for i in $(seq 1 60); do "
            "  curl -sf http://localhost:8899/api >/dev/null 2>&1 && READY=1 && break; "
            "  echo \"[$i/60] waiting for Jupyter... 5s\"; sleep 5; "
            "done && "
            "[ \"$READY\" -eq 1 ] || "
            "{ echo 'ERROR: Jupyter did not start in 5 min'; "
            "  docker stop %(name)s 2>/dev/null; exit 1; } && "
            "echo 'Jupyter ready.'"
        ) % {"name": container_name}
