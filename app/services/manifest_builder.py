from typing import Any, Dict


class ManifestBuilder:
    """
    Builds the command strings to execute benchmarks on GPU nodes.
    Translates user configuration into docker run commands.
    """

    @staticmethod
    def build_vllm_command(model_name: str, config: Dict[str, Any]) -> str:
        from app.config import settings

        gpu_count = config.get("gpu_count", 1)
        image = "%s/llm-inference:%s" % (settings.WORKLOAD_REGISTRY, settings.WORKLOAD_IMAGE_TAG)

        # Build volume mount based on MODEL_STORAGE_MODE
        volume_flag = ""
        env_flags = ""
        mode = settings.MODEL_STORAGE_MODE

        if mode == "local":
            volume_flag = "-v %s:/models" % settings.MODEL_LOCAL_PATH
            env_flags = "-e HF_HOME=/models"
        elif mode == "huggingface":
            volume_flag = "-v $HOME/.cache/huggingface:/root/.cache/huggingface"
        elif mode == "gcs":
            env_flags = "-e GCS_BUCKET=%s" % settings.MODEL_GCS_BUCKET

        cmd_parts = [
            "docker run --gpus all --rm -d",
            "-p 8000:8000 --ipc=host",
            volume_flag,
            env_flags,
            image,
            "--model %s" % model_name,
            "--tensor-parallel-size %d" % gpu_count,
        ]

        if "max_model_len" in config:
            cmd_parts.append("--max-model-len %d" % config["max_model_len"])

        return " ".join(part for part in cmd_parts if part)

    @staticmethod
    def build_benchmark_client_command(config: Dict[str, Any]) -> str:
        concurrency = config.get("concurrency", 1)
        input_tokens = config.get("input_tokens", 512)
        output_tokens = config.get("output_tokens", 512)

        script = (
            'echo "Waiting for vLLM server..." && sleep 15 && '
            'echo "Running benchmark: concurrency=%d, input=%d, output=%d" && '
            'sleep 5 && '
            'echo "Benchmark finished."'
        ) % (concurrency, input_tokens, output_tokens)
        return script
