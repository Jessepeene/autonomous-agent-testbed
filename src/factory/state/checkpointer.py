"""LangGraph checkpointing.

Graph state lives in Postgres so a node that was mid-flight when the process died
resumes on the next boot instead of being lost. The in-memory saver exists only for
tests; it is never selected implicitly in production because losing checkpoints
silently would violate the "no task may be lost after reboot" rule.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from factory.logging import get_logger
from factory.settings import Settings, get_settings
from factory.state.db import psycopg_dsn

log = get_logger(__name__)


@asynccontextmanager
async def postgres_checkpointer(settings: Settings | None = None) -> AsyncIterator[Any]:
    """Yield a migrated AsyncPostgresSaver backed by a managed connection pool.

    ``AsyncPostgresSaver.from_conn_string()`` opens exactly one unpooled connection
    and never recycles it. Cloud-hosted Postgres enforces its own idle-connection
    timeout — commonly well under an hour — and closes connections that sit idle past
    it; the driver has no way to find out until the next query fails with
    ``psycopg.OperationalError``, hours into an unattended run. An explicit
    ``AsyncConnectionPool`` with ``max_idle``/``max_lifetime`` set below any realistic
    server-side timeout means connections are recycled on our schedule, proactively,
    before the server ever gets to drop one out from under a live checkpoint write.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import DictRow, dict_row
    from psycopg_pool import AsyncConnectionPool

    settings = settings or get_settings()
    dsn = psycopg_dsn(settings)

    # Connection kwargs match `from_conn_string` exactly: `autocommit=True` and
    # `prepare_threshold=0` are required by `AsyncPostgresSaver`'s own cursor usage,
    # and `dict_row` is required because its `setup()`/`aget_tuple()` etc. index rows
    # by column name, not position. `connection_class` is parametrized to match, so
    # the pool's declared row type and its actual runtime row type agree.
    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        dsn,
        connection_class=AsyncConnection[DictRow],
        min_size=settings.checkpointer_pool_min_size,
        max_size=settings.checkpointer_pool_max_size,
        max_idle=settings.checkpointer_pool_max_idle_seconds,
        max_lifetime=settings.checkpointer_pool_max_lifetime_seconds,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=False,
    )
    await pool.open(wait=True, timeout=30.0)
    try:
        saver = AsyncPostgresSaver(conn=pool)
        await saver.setup()
        log.info(
            "checkpointer.ready",
            backend="postgres",
            pooled=True,
            max_idle_s=settings.checkpointer_pool_max_idle_seconds,
            max_lifetime_s=settings.checkpointer_pool_max_lifetime_seconds,
        )
        yield saver
    finally:
        await pool.close()


@asynccontextmanager
async def memory_checkpointer() -> AsyncIterator[Any]:
    """Tests only. Nothing survives the process."""
    from langgraph.checkpoint.memory import InMemorySaver

    yield InMemorySaver()
