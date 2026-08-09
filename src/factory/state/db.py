"""Async engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from factory.settings import Settings, get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class DatabaseNotConfigured(RuntimeError):
    """Raised when DATABASE_URL is absent. Preflight should have caught this first."""


def sqlalchemy_url(settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.database_url is None:
        raise DatabaseNotConfigured("DATABASE_URL is not set")
    url = settings.database_url.get_secret_value()
    if not url:
        raise DatabaseNotConfigured("DATABASE_URL is empty")
    return url


def psycopg_dsn(settings: Settings | None = None) -> str:
    """LangGraph's Postgres checkpointer speaks psycopg, not asyncpg.

    Both point at the same database; only the driver prefix differs.
    """
    url = sqlalchemy_url(settings)
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        resolved = settings or get_settings()
        _engine = create_async_engine(
            sqlalchemy_url(settings),
            pool_pre_ping=True,
            # Proactive: never hand out a connection older than this, regardless of
            # health. Complements `pool_pre_ping` (reactive — checks liveness right
            # before handing a connection out) so a cloud provider's own idle timeout
            # is never the thing that first discovers a dead connection.
            pool_recycle=resolved.db_pool_recycle_seconds,
            pool_size=10,
            max_overflow=10,
            future=True,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    get_engine(settings)
    assert _sessionmaker is not None
    return _sessionmaker


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """Transactional scope. Commits on success, rolls back on any exception."""
    maker = get_sessionmaker(settings)
    session = maker()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
