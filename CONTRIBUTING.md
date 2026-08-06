# Contributing to AIStudio Server

Thanks for your interest in contributing. This document covers how to get started, what areas are open for extension, and what to verify before opening a pull request.

## Getting Started

1. Fork the repo and create a branch from `master`.
2. Set up your local environment — see the [README](README.md) for prerequisites (PostgreSQL, RabbitMQ, Python deps).
3. Run the test suite to confirm everything passes before making changes.
4. Open a pull request against `master` with a clear description of what and why.

For significant changes (new workload types, schema changes, new API endpoints), open an issue first to discuss the approach before writing code.

## Areas Open for Extension

| Area | Examples |
|------|---------|
| **New workload types** | Add support for training jobs, embedding benchmarks, diffusion model inference |
| **GPU vendor support** | AMD ROCm nodes, Intel Gaudi — the manifest builder and SSH executor are the right entry points |
| **Model catalog** | Add new HuggingFace models to `catalog.json` with correct license and gating metadata |
| **Benchmark metrics** | Extend `benchmark.py` cost engine or add new metric types to the result schema |
| **Dashboard** | New charts or filters in the FastAPI dashboard (`dashboard/backend/`) |
| **Storage backends** | S3/GCS dataset and result storage alongside the existing NFS support |
| **CI/CD** | Additional GitHub Actions workflows for linting, security scanning, or test coverage |

## Best Practices

- **One concern per PR.** Keep changes focused — a new workload type should not also refactor the SSH executor.
- **Tests required.** New API endpoints, services, and state machine transitions need tests in `AIStudio/tests/`.
- **No secrets in code.** Tokens, passwords, and keys belong in `~/.aistudio/env` on GPU nodes or in Helm values secrets — never hardcoded or logged.
- **License compliance.** Any new dependency must have a permissive licence (Apache-2.0, MIT, BSD). Update `THIRD-PARTY-NOTICES.md` if you add a package with notice obligations. Do not add packages with GPL, AGPL, or contested provenance without prior discussion.
- **Workload isolation.** Workload containers must remain self-contained — no direct database access, all communication via RabbitMQ events and the REST API.

## Before Raising a PR

Run these checks locally and confirm they all pass:

```bash
# 1. Full test suite (requires PostgreSQL — see README for setup)
cd AIStudio
pytest tests/ -v --log-cli-level=INFO

# 2. License compliance check
make check-licenses

# 3. Model access check (verifies catalog.json entries are reachable)
python3 scripts/check_model_access.py

# 4. Node environment check (if you changed manifest_builder or SSH execution)
make check-node-env NODE=<your-gpu-node>
```

CI runs the test suite and compliance checks automatically on every PR. PRs with failing tests will not be merged.

## Reporting Bugs

Open a GitHub issue with: steps to reproduce, expected vs actual behaviour, relevant log output, and your deployment environment (Kubernetes version, GPU type, Python version).

## Security Issues

Do not open a public issue for security vulnerabilities. Email [muraharirao@corespan.ai](mailto:muraharirao@corespan.ai) directly.
