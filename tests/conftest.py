"""
Shared pytest fixtures for aistudio-server tests.

Requires a local PostgreSQL instance. The test database ('aistudio_test') is
created automatically on first run.

Start PostgreSQL before running:
    docker compose up -d postgres

Run all tests:
    pytest tests/ -v
"""

import asyncio
import os

# ── Point at the test DB BEFORE any app module is imported ─────────────────
# pydantic-settings reads env vars at class-body time; lru_cache must be
# cleared if settings was already cached by a prior import.
# ── PostgreSQL connection ─────────────────────────────────────────────────────
# Two execution contexts:
#
#   make test  (inside the api container — `docker compose exec api pytest tests/`)
#     POSTGRES_HOST=postgres  already set by docker-compose environment block
#     POSTGRES_PORT           NOT set by docker-compose → Settings default of 5432 is used
#
#   pytest directly on the host machine
#     docker-compose maps postgres to host port 5433 (5433:5432).
#     Run as:  POSTGRES_PORT=5433 pytest tests/ -v
#
# We do NOT setdefault POSTGRES_PORT here so that Settings.POSTGRES_PORT keeps
# its built-in default of 5432, which is correct inside the container.
# Host-side callers must pass POSTGRES_PORT=5433 explicitly.
os.environ.setdefault("POSTGRES_HOST",     "localhost")
os.environ.setdefault("POSTGRES_USERNAME", "aistudio")
os.environ.setdefault("POSTGRES_PASSWORD", "aistudio")
# Force (not setdefault) — this MUST be an isolated database, never whatever
# POSTGRES_DATABASE the app's own .env configures. Inside the api container,
# docker-compose's `env_file: .env` already sets POSTGRES_DATABASE=aistudio
# (the real dev/seed database) before pytest ever runs, which would make
# setdefault() here a silent no-op. clean_tables below TRUNCATEs every table
# before every test — running that against the real database wipes seeded
# data instead of a disposable one.
os.environ["POSTGRES_DATABASE"] = "aistudio_test"
os.environ.setdefault("RABBITMQ_URL",      "localhost")
os.environ.setdefault("RABBITMQ_USERNAME", "aistudio")
os.environ.setdefault("RABBITMQ_PASSWORD", "aistudio")

# Clear the lru_cache so Settings re-reads our env vars
from app.config import get_settings
get_settings.cache_clear()

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import Base, get_db

# ── Test DB connection strings ──────────────────────────────────────────────
_PG_USER  = os.environ["POSTGRES_USERNAME"]
_PG_PASS  = os.environ["POSTGRES_PASSWORD"]
_PG_HOST  = os.environ["POSTGRES_HOST"]
_PG_PORT  = os.environ["POSTGRES_PORT"]
_TEST_DB  = os.environ["POSTGRES_DATABASE"]

_ADMIN_SYNC_URL = f"postgresql://{_PG_USER}:{_PG_PASS}@{_PG_HOST}:{_PG_PORT}/postgres"
_TEST_ASYNC_URL = f"postgresql+asyncpg://{_PG_USER}:{_PG_PASS}@{_PG_HOST}:{_PG_PORT}/{_TEST_DB}"


# ── Create the test database once (outside pytest fixtures) ─────────────────
def _ensure_test_db_exists() -> None:
    engine = create_engine(_ADMIN_SYNC_URL, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"),
            {"name": _TEST_DB},
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{_TEST_DB}"'))
    engine.dispose()


_ensure_test_db_exists()


# ── Session-scoped event loop ────────────────────────────────────────────────
@pytest.fixture(scope="session")
def event_loop():
    """Single event loop shared across the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ── Create / drop tables once per session ────────────────────────────────────
@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    """DDL run once: create all ORM-mapped tables in the test DB."""
    engine = create_async_engine(_TEST_ASYNC_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ── Truncate all tables before every test ────────────────────────────────────
@pytest_asyncio.fixture(autouse=True)
async def clean_tables():
    """Wipe all rows before each test so tests are fully isolated."""
    engine = create_async_engine(_TEST_ASYNC_URL, echo=False)
    async with engine.begin() as conn:
        # Reversed sorted_tables respects FK order; CASCADE handles the rest.
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(
                text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE')
            )
    await engine.dispose()
    yield


# ── Direct DB session for test setup ────────────────────────────────────────
@pytest_asyncio.fixture
async def db_session() -> AsyncSession:
    """
    Async SQLAlchemy session wired to the test DB.

    Use for inserting seed data in tests that need pre-existing rows.
    Remember to call ``await db_session.commit()`` after inserts so the
    HTTP client (which opens its own session) can read them.
    """
    engine = create_async_engine(_TEST_ASYNC_URL, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ── HTTPX client wired to the test DB ────────────────────────────────────────
@pytest_asyncio.fixture
async def http_client(db_session: AsyncSession):
    """
    Async HTTPX client pointing at the FastAPI app.

    Overrides the ``get_db`` dependency so every request handler uses the
    same test-DB session as the ``db_session`` fixture — inserts made in a
    test are visible to the HTTP handler without a separate commit step.
    """
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client
    app.dependency_overrides.clear()
