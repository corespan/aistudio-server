from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

# The app instance
app = FastAPI(
    title="AIStudio API",
    description="Benchmarking Engine for LLM Inference",
    version="1.0.0",
)

# ── Middleware ───────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    """
    Ensure the UI never caches API responses.
    This guarantees the dashboard always shows live DB state.
    """
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

# ── Exception Handlers ────────────────────────────────────────────────────────
# Enforces the standard error shape: {"status": "error", "code": X, "detail": "...", "hint": "..."}

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    msg = errors[0]["msg"] if errors else "Validation failed"
    loc = errors[0]["loc"] if errors else []
    
    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "code": 422,
            "detail": f"Field '{loc[-1] if loc else 'unknown'}' error: {msg}",
            "hint": "Check the request schema."
        },
    )

@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "code": 500,
            "detail": "An internal server error occurred.",
            "hint": str(exc) # For debugging. In production, this should be masked.
        },
    )

from app.routers import system, ingest, benchmarks, results, jupyter, gpu_specs

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(system.router)
app.include_router(ingest.router)
app.include_router(benchmarks.router)
app.include_router(results.router)
app.include_router(gpu_specs.router)
app.include_router(jupyter.router)
