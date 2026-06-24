from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import get_database_url, get_sync_database_url


# Async Engine
engine = create_async_engine(
    get_database_url(),
    echo=False,
    pool_pre_ping=True,
)

# Sync Engine (For Celery)
sync_engine = create_engine(
    get_sync_database_url(),
    echo=False,
    pool_pre_ping=True,
)
SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    expire_on_commit=False,
)

# Async Session Factory
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# Declarative Base
class Base(DeclarativeBase):
    pass


# FastAPI Dependency
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
