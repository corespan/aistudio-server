"""
app/catalog.py — Model catalog for AIStudio (open-source edition).

Defines the models Corespan provides via GCR images, along with their
default vLLM configuration parameters.  Shared by both the system router
(for the /models/config endpoint) and the results router (for /models, which
must always return catalog models regardless of past run history).
"""

# Default vLLM configs per model.
# P40-safe defaults use fp32 since the P40 (Pascal) has no native FP16 tensor cores.
_MODEL_CONFIGS: dict = {
    "tinyllama/tinyllama-1.1b-chat-v1.0": {
        "precision": "fp32",
        "concurrency": 4,
        "input_tokens": 512,
        "output_tokens": 128,
        "max_model_len": 2048,
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "batch_size": 32,
    },
    "llama3-8b-instruct": {
        "precision": "fp16",
        "concurrency": 8,
        "input_tokens": 512,
        "output_tokens": 256,
        "max_model_len": 4096,
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "batch_size": 32,
    },
    "mistral-7b-instruct": {
        "precision": "fp16",
        "concurrency": 8,
        "input_tokens": 512,
        "output_tokens": 256,
        "max_model_len": 4096,
        "tensor_parallel_size": 1,
        "pipeline_parallel_size": 1,
        "batch_size": 32,
    },
}

_DEFAULT_CONFIG: dict = {
    "precision": "fp16",
    "concurrency": 4,
    "input_tokens": 512,
    "output_tokens": 256,
    "max_model_len": 4096,
    "tensor_parallel_size": 1,
    "pipeline_parallel_size": 1,
    "batch_size": 32,
}
