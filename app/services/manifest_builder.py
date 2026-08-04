from typing import Any, Dict

from app.config import settings, get_workload_registry

# The aistudio GCR repository (us-docker.pkg.dev/aimlworkbench/aistudio) has public
# read access — no login is required on GPU nodes to pull images.

# ── Node environment prelude ─────────────────────────────────────────────────
# Loads per-node secrets before any docker command runs. Currently that is
# HF_TOKEN, required to download gated models — see MODEL-LICENSES.md.
#
# This exists because the obvious approach does not work. Commands here are run
# via SSHExecutor's exec_command(), which sshd executes as `$SHELL -c '<cmd>'`.
# That is a NON-INTERACTIVE shell, and Debian/Ubuntu's stock ~/.bashrc opens
# with:
#
#     case $- in
#         *i*) ;;
#           *) return;;
#     esac
#
# so anything appended to ~/.bashrc — the natural place to put an export, and
# what most setup instructions tell you to do — sits below that early `return`
# and never runs for our commands. The variable would appear set when the
# operator SSHes in by hand and unset for every benchmark, which is a
# maddening way to discover the problem.
#
# A dedicated file sourced explicitly avoids the whole question. It also keeps
# the token out of the interactive environment of anyone who logs into the node.
_NODE_ENV_FILE = "$HOME/.aistudio/env"
_LOAD_NODE_ENV = (
    "if [ -f %s ]; then set -a; . %s; set +a; fi" % (_NODE_ENV_FILE, _NODE_ENV_FILE)
)


def _hf_token_flags() -> str:
    """Docker flags that forward the node's HF_TOKEN into the workload container.

    Uses docker's passthrough form (`-e VAR` with no `=value`), which reads the
    value from the daemon client's environment. That avoids interpolating the
    secret into a shell string entirely — no quoting hazard, and the token never
    appears in the command text that gets logged or stored in a run manifest.

    Expands to nothing when HF_TOKEN is unset, so behaviour is unchanged for
    ungated models and for weights already present in the mounted cache.

    HUGGING_FACE_HUB_TOKEN is the older name; huggingface_hub still honours both
    and vLLM images in the wild read one or the other depending on vintage.
    """
    return (
        '${HF_TOKEN:+-e HF_TOKEN} '
        '${HF_TOKEN:+-e HUGGING_FACE_HUB_TOKEN="$HF_TOKEN"}'
    )


class ManifestBuilder:
    """
    Builds the shell command strings executed on GPU nodes via SSHExecutor.

    LLM benchmarks run as a single container via build_llm_benchmark_command():
    the image's benchmark.py starts its own vLLM server, sweeps concurrency
    levels, computes cost estimates, and prints BENCH_RESULT:{json} lines that
    execute_benchmark() parses to populate BenchmarkResult rows.
    """

    @staticmethod
    def build_llm_benchmark_command(model_name: str, config: Dict[str, Any],
                                    image_tag: str = None, run_id: str = "") -> str:
        """
        Run the llminference image's benchmark.py on the GPU node.

        benchmark.py owns the entire workflow:
          1. Starts its own vLLM server (port 9123, internal to the container)
          2. Sweeps the requested concurrency levels via `vllm bench serve`
          3. Attaches GPU power/utilisation metrics (if enabled)
          4. Computes cost estimates via costEngine + hardware_pricing.json
          5. Writes results to /results/{run_id}/ and prints BENCH_RESULT: lines

        The container is removed when the run exits (--rm), so the node is clean
        for the next job without needing a separate cleanup step.

        Volume mounts (bind-mounted from the GPU node):
          ~/.cache/huggingface → /root/.cache/huggingface  (model cache; downloaded on first run)
          NODE_RESULTS_PATH   → /results                   (one subdir per run_id is created)

        HF_TOKEN is forwarded from the node's ~/.aistudio/env file via
        _LOAD_NODE_ENV. It is never written into this command string.
        """
        gpu_count     = config.get("gpu_count", 1)
        concurrency   = config.get("concurrency", 32)
        enable_opts   = config.get("enable_optimizations", False)
        max_model_len = config.get("max_model_len")

        tag   = image_tag or settings.WORKLOAD_IMAGE_TAG
        image = "%s/llminference:%s" % (get_workload_registry(), tag)

        # Container name is inspectable via `docker ps -a` on the node.
        # The container is removed on exit (--rm), so no cleanup step needed.
        container_name = "llmbench-%s" % run_id if run_id else "llmbench"
        rm_cmd = "docker rm -f %s 2>/dev/null || true" % container_name

        bench_args = [
            "--model",            model_name,
            "--output_data_path", "/results/%s" % run_id,
            "--run_name",         run_id or model_name,
            "--tp",               str(gpu_count),
            "--concurrencies",    str(concurrency),
        ]
        if max_model_len:
            bench_args += ["--max_model_len", str(max_model_len)]
        if enable_opts:
            bench_args.append("--enable_optimizations")

        # Mount the node's HuggingFace cache so models are used from local cache
        # when already downloaded, and downloaded+cached on first run.
        # Mount results dir so benchmark output persists after --rm.
        run_cmd = " ".join(p for p in [
            "docker run --gpus all --rm --name %s" % container_name,
            "--ipc=host",
            "-v $HOME/.cache/huggingface:/root/.cache/huggingface",
            "-v %s:/results" % settings.NODE_RESULTS_PATH,
            _hf_token_flags(),
            image,
            "python3 /llm-inference/benchmark.py",
            " ".join(bench_args),
        ] if p)

        return "%s ; %s && %s" % (_LOAD_NODE_ENV, rm_cmd, run_cmd)

    @staticmethod
    def build_jupyter_command(run_id: str = "", base_url: str = "") -> str:
        """
        Start the jupyternotebook GCR image on the node.

        Runs the container's CMD (script.sh → startJupyter.py) directly — no
        entrypoint override. script.sh copies notebooks to NFS at /data/{workload_id}/
        and startJupyter.py launches Jupyter from that directory so:
          - Only notebooks are visible (no Dockerfile/script.sh/etc.)
          - All user edits persist on NFS

        base_url — when set (e.g. "/jupyter/T4/jup-20260729-xxx/"), the container
        receives JUPYTER_BASE_URL as an env var. startJupyter.py must honour it by
        passing --ServerApp.base_url=$JUPYTER_BASE_URL to the jupyter lab command.
        This is required for JupyterLab to work correctly behind a subpath proxy.
        When NGINX_ENABLED=false the arg is omitted and Jupyter runs at root (/lab).
        """
        image = "%s/jupyternotebook:%s" % (get_workload_registry(), settings.JUPYTER_IMAGE_TAG)
        container_name = "jupyter-%s" % run_id if run_id else "jupyter_server"

        rm_cmd = "docker rm -f %s 2>/dev/null || true" % container_name
        # Pass workload_id and port as env vars so script.sh and startJupyter.py
        # write notebooks to the correct NFS path (/data/{workload_id}/).
        # $JUPYTER_PORT is resolved at runtime by the worker before this command runs.
        # JUPYTER_BASE_URL tells startJupyter.py which subpath to mount Jupyter at
        # (needed when nginx proxies requests under a non-root path).
        env_parts = [
            "-e workload_id=%s" % run_id,
            "-e workload_port=7008",
        ]
        if base_url:
            env_parts.append("-e JUPYTER_BASE_URL=%s" % base_url)

        run_cmd = " ".join([
            "docker run --gpus all -d --name %s" % container_name,
            "-p $JUPYTER_PORT:7008 --ipc=host",
            "-v /data:/data",
        ] + env_parts + [image])
        # _LOAD_NODE_ENV first: it populates HF_TOKEN from the node's env file so
        # the ${HF_TOKEN:+...} expansions in run_cmd resolve. See its definition
        # for why ~/.bashrc cannot be used for this.
        return "%s ; %s && %s" % (_LOAD_NODE_ENV, rm_cmd, run_cmd)

    @staticmethod
    def build_jupyter_health_command(run_id: str = "", base_url: str = "") -> str:
        """Poll localhost:$JUPYTER_PORT until Jupyter is ready (up to 5 min).
        base_url must match --ServerApp.base_url so the health check hits the right path.
        """
        container_name = "jupyter-%s" % run_id if run_id else "jupyter_server"
        # Jupyter API endpoint respects the base_url prefix.
        # e.g. base_url=/jupyter/T4/jup-xxx/ → check /jupyter/T4/jup-xxx/api
        api_path = (base_url.rstrip("/") + "/api") if base_url and base_url != "/" else "/api"
        return (
            "READY=0 && "
            "for i in $(seq 1 300); do "
            "  curl -sf http://localhost:$JUPYTER_PORT%(api_path)s >/dev/null 2>&1 && READY=1 && break; "
            "  [ $((i %% 10)) -eq 0 ] && echo \"[$i/300] waiting for Jupyter...\"; sleep 1; "
            "done && "
            "[ \"$READY\" -eq 1 ] || "
            "{ echo 'ERROR: Jupyter did not start in 5 min'; "
            "  docker stop %(name)s 2>/dev/null; exit 1; } && "
            "echo 'Jupyter ready.'"
        ) % {"name": container_name, "api_path": api_path}
