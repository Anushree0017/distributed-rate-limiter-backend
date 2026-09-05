"""SQLAlchemy async engine/session setup for the rules-CRUD service.

One `AsyncEngine` for the process lifetime (built lazily on first use, mirroring
`core/redis_client.py`'s "one pool for the process, never per-request" convention),
plus `get_db`, the FastAPI dependency that hands each request its own `AsyncSession`.
"""
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from core.settings import get_database_url

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models in `model/`."""


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_database_url(), pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: one `AsyncSession` per request, closed afterward."""
    async with get_session_factory()() as session:
        yield session


async def dispose_engine() -> None:
    """Close the engine's connection pool on app shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
