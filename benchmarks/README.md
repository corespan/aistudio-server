# Benchmark Reproducibility Scripts

Each script in this folder reproduces a specific run on the AIStudio leaderboard.
Run them on compatible hardware to verify the published numbers.

## Structure

```
benchmarks/
  mi210/          AMD Instinct MI210 runs (ROCm, SuperMicro server)
  h100/           NVIDIA H100 NVL runs   (CUDA, PRU server)
  rtx5090/        NVIDIA RTX 5090 runs   (CUDA, PRU server)
  t4/             NVIDIA T4 runs         (CUDA, Dell server)
```

## How to use

1. Pick the script matching the hardware you want to reproduce
2. Make it executable: `chmod +x benchmarks/<gpu>/<script>.sh`
3. Run it: `./benchmarks/<gpu>/<script>.sh`

The script starts the inference server, runs the benchmark, prints results, and cleans up.

## Requirements

- Docker with GPU access (`--gpus` flag or ROCm device passthrough)
- HuggingFace model cache at `~/.cache/huggingface`
- For MI210 scripts: ROCm driver ≥ 6.1
- For NVIDIA scripts: CUDA driver ≥ 12.4, `nvidia-container-toolkit`
- For RTX 5090 ApacheBench scripts: `ab` (Apache HTTP server tools)

## Scripts

| Script | Model | Hardware | Concurrencies | Peak tok/s |
|--------|-------|----------|---------------|-----------|
| `mi210/llama31-70b-tp4.sh` | Llama-3.1-70B | 4× MI210 TP=4 | 4, 8, 16 | 366 |
| `mi210/llama31-70b-tp8.sh` | Llama-3.1-70B | 8× MI210 TP=8 | 4, 8, 16 | 470 |
| `mi210/deepseek-r1-70b-tp8.sh` | DeepSeek-R1-70B | 8× MI210 TP=8 | 32, 64, 128 | 2112 |
| `mi210/llama33-70b-tp8.sh` | Llama-3.3-70B | 8× MI210 TP=8 | 4, 8, 16 | 410 |
| `h100/llama31-70b-c128.sh` | Llama-3.1-70B | 2× H100 NVL TP=2 | 128 | 2545 |
| `h100/llama31-70b-c256.sh` | Llama-3.1-70B | 2× H100 NVL TP=2 | 256 | 2255 |
| `rtx5090/qwen25-32b-tp4-bf16.sh` | Qwen2.5-32B | 4× RTX 5090 TP=4 BF16 | 20 | 860 |
| `rtx5090/qwen25-32b-pp4-fp8.sh` | Qwen2.5-32B | 4× RTX 5090 PP=4 FP8 | 20 | 5345 |
| `t4/tinyllama-tp1.sh` | TinyLlama-1.1B | 1× T4 TP=1 | 4, 8, 16 | 1477 |
| `t4/tinyllama-tp2.sh` | TinyLlama-1.1B | 2× T4 TP=2 | 4, 8, 16 | 1794 |
