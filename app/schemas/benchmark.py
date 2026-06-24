from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── 1. Legacy Ingestion Schema (POST /api/v1/metrics) ───────────────────────
# This perfectly matches the old JSON payload sent by existing Kubeflow/CLI runners.
# The router will receive this, map the fields, and insert into the new Postgres table.

class LegacyWorkloadObj(BaseModel):
    name: str = Field(..., description="e.g. 'llama3-8b-instruct'")
    type: str = Field(..., description="e.g. 'llm' or 'cv'")


class BenchmarkIngestPayload(BaseModel):
    run_id: str
    timestamp: datetime
    workload: LegacyWorkloadObj
    metrics: Dict[str, Any]
    status: str
    gpu_type: str
    node_ip: str
    config: Dict[str, Any]
    
    # Optional field in case we DO want to send it in the future,
    # defaults to 0 for backward compatibility with old runners.
    sub_run_index: int = 0


# ── 2. AIStudio Start Request Schema (POST /api/v1/benchmarks/start) ────────

class BenchmarkStartRequest(BaseModel):
    model_name: str = Field(..., description="LLM model to benchmark, e.g. 'llama3-8b-instruct'")
    node_ips: List[str] = Field(..., description="List of IPs to run the benchmark on")
    
    # E.g. {"concurrency": 8, "input_tokens": 512, "output_tokens": 512}
    config: Dict[str, Any] = Field(default_factory=dict, description="Benchmark parameters")


class BenchmarkStartResponse(BaseModel):
    status: str
    task_id: str = Field(..., description="The workload_id to use for polling status and logs")
    message: str
