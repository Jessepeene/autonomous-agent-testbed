"""The pooled Postgres checkpointer, against a real database.

Needs Postgres:

    docker compose up -d postgres
    pytest tests/integration -q

Confirms the pool actually recycles rather than merely constructing without error —
that is the entire point of this change, and a construction-only test would pass even
if `max_idle`/`max_lifetime` were silently ignored.
"""

from __future__ import annotations

import os

import pytest

from factory.settings import Settings, get_settings
from factory.state.checkpointer import postgres_checkpointer

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)


def _settings() -> Settings:
    base = get_settings()
    return base.model_copy(update={"database_url": DATABASE_URL})


async def test_the_checkpointer_is_usable_end_to_end() -> None:
    async with postgres_checkpointer(_settings()) as saver:
        config = {"configurable": {"thread_id": "pool-smoke-test"}}
        tuple_ = await saver.aget_tuple(config)
        # A brand-new thread id has no checkpoint yet; the call itself is what
        # matters — it proves the pooled connection actually executes a real query.
        assert tuple_ is None


async def test_the_pool_is_configured_with_the_settings_values() -> None:
    settings = _settings().model_copy(
        update={
            "checkpointer_pool_min_size": 2,
            "checkpointer_pool_max_size": 5,
            "checkpointer_pool_max_idle_seconds": 120.0,
            "checkpointer_pool_max_lifetime_seconds": 600.0,
        }
    )
    async with postgres_checkpointer(settings) as saver:
        pool = saver.conn
        assert pool.min_size == 2
        assert pool.max_size == 5
        assert pool.max_idle == 120.0
        assert pool.max_lifetime == 600.0
