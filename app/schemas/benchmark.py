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


# ── 3. Leaderboard Response Schema (GET /api/v1/benchmarks) ─────────────────
#
# Explicit contract so the frontend knows exactly which fields to expect.
# Key fields for chart rendering:
#   gpu_count    → group into separate series per GPU count (4×, 8×, etc.)
#   server_name  → filter dropdown
#   per_gpu_throughput_tok_s  → Y-axis for "tok/s per GPU" view (InferenceX style)
#   metrics      → full blob for tooltip details (parallelism, chunked_prefill, etc.)

class BenchmarkResultResponse(BaseModel):
    """
    Leaderboard row returned by GET /api/v1/benchmarks.

    Only hot columns are included — no raw metrics blob.
    The full JSONB metrics payload is available on the single-run
    detail endpoint: GET /api/v1/benchmarks/{run_id}.

    per_gpu_throughput_tok_s is computed server-side (total ÷ gpu_count)
    so the frontend can plot an InferenceX-style tok/s/GPU axis without
    needing to do the division itself.
    """

    model_config = {"from_attributes": True}

    run_id: str
    sub_run_index: int

    # Workload discriminator
    workload_type: str

    # Model & pipeline
    model_name: str
    pipeline_version: str

    # Hardware
    node_ips: List[str]
    gpu_type: str
    gpu_count: int
    gpu_model: Optional[str]
    server_name: Optional[str]

    # Config
    precision: str
    input_tokens: int
    output_tokens: int
    concurrency: int

    # Status
    status: str

    # Hot metrics
    total_token_throughput: Optional[float]
    mean_ttft_ms: Optional[float]
    mean_tpot_ms: Optional[float]
    mean_e2el_ms: Optional[float]

    # Server-computed derived field
    per_gpu_throughput_tok_s: Optional[float] = Field(
        None,
        description="total_token_throughput ÷ gpu_count. Null if throughput is missing.",
    )

    # Timestamps
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_seconds: Optional[float]
    created_at: datetime

    @classmethod
    def from_orm_row(cls, row: Any) -> "BenchmarkResultResponse":
        """Build from a BenchmarkResult ORM object, computing per_gpu_throughput."""
        data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        tpt = data.get("total_token_throughput")
        gc = data.get("gpu_count") or 1
        data["per_gpu_throughput_tok_s"] = round(tpt / gc, 2) if tpt else None
        return cls(**data)


# ── 4. Detail Response Schema (GET /api/v1/benchmarks/{run_id}) ─────────────

class BenchmarkDetailSubRun(BenchmarkResultResponse):
    """
    Single sub-run row in a detail response.
    Adds parallelism and raw metrics blob (detail-only fields).
    """

    # Detail-only hot column — compact parallelism string e.g. 'tp4', 'pp4'
    parallelism: Optional[str] = None

    # Full raw metrics JSONB blob from the runner
    metrics: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_orm_row(cls, row: Any) -> "BenchmarkDetailSubRun":
        """Build from ORM row, including parallelism and metrics."""
        data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
        tpt = data.get("total_token_throughput")
        gc = data.get("gpu_count") or 1
        data["per_gpu_throughput_tok_s"] = round(tpt / gc, 2) if tpt else None
        return cls(**data)


class BenchmarkDetailResponse(BaseModel):
    """
    Full detail response for GET /api/v1/benchmarks/{run_id}.
    Groups all sub-runs under a single run_id.
    """
    run_id: str
    sub_runs: List[BenchmarkDetailSubRun]
