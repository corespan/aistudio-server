from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


# ── Async Engine ──────────────────────────────────────────────────────────────
# The engine is the core connection pool to PostgreSQL.
# 'echo=False' in production — set to True temporarily to see raw SQL in logs.
# 'pool_pre_ping=True' — tests the connection before using it from the pool.
# This avoids "connection closed" errors after the DB restarts or goes idle.
engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
)

# ── Sync Engine (For Celery) ──────────────────────────────────────────────────
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sync_engine = create_engine(
    settings.sync_database_url,
    echo=False,
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    expire_on_commit=False,
)


# ── Session Factory ───────────────────────────────────────────────────────────
# AsyncSessionLocal is a factory — each call to AsyncSessionLocal() creates
# a new session tied to a single request lifecycle.
# expire_on_commit=False means model attributes remain accessible after
# the session commits, which is important for returning data in the response.
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Declarative Base ──────────────────────────────────────────────────────────
# All SQLAlchemy ORM models in app/models/ inherit from this Base.
# It holds the metadata (table definitions) that Alembic reads to generate migrations.
class Base(DeclarativeBase):
    pass


# ── FastAPI Dependency ────────────────────────────────────────────────────────
async def get_db() -> AsyncSession:
    """
    FastAPI dependency that yields a database session for the duration
    of a single request, then closes it automatically.

    Usage in a router:
        from app.database import get_db
        from sqlalchemy.ext.asyncio import AsyncSession

        @router.get("/example")
        async def example(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(SomeModel))
            return result.scalars().all()

    The 'async with' block ensures the session is always closed —
    even if an exception is raised mid-request.
    """
    async with AsyncSessionLocal() as session:
        yield session
