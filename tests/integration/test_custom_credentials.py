"""`POST /dashboard/integrations/credentials` -- the free-form counterpart to
the catalog-only `POST /dashboard/integrations/credentials/{key}`
(`test_dashboard_auth.py`), for a credential this factory has no catalog
entry or adapter code for yet. Real DB round trip: `custom_credentials`
(migration 0029) registers the key, `.env` holds the real value.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.observability import integrations as integrations_metrics
from factory.settings import Settings, get_settings
from factory.state import repository
from factory.state.orm import Base

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(DATABASE_URL or "", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
        await db.rollback()
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _reset_global_engine() -> AsyncIterator[None]:
    """The endpoint opens sessions through the process-wide cached engine, a
    different connection than this file's own `session` fixture -- see
    test_analytics_beacon.py's identical fixture for why this reset matters."""
    yield
    from factory.state import db as db_module

    await db_module.dispose_engine()
    get_settings.cache_clear()


async def _client() -> httpx.AsyncClient:
    from factory.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- repository.create_custom_credential / list_custom_credentials --------------------


async def test_create_custom_credential_returns_a_new_id(session: AsyncSession) -> None:
    credential_id = await repository.create_custom_credential(session, "MY_NEW_KEY")

    assert credential_id is not None


async def test_re_adding_the_same_key_is_an_idempotent_no_op(session: AsyncSession) -> None:
    first_id = await repository.create_custom_credential(session, "MY_NEW_KEY")
    assert first_id is not None

    second_id = await repository.create_custom_credential(session, "MY_NEW_KEY")

    assert second_id is None
    rows = await repository.list_custom_credentials(session)
    assert len([r for r in rows if r.key == "MY_NEW_KEY"]) == 1


async def test_list_custom_credentials_returns_every_registered_key(session: AsyncSession) -> None:
    await repository.create_custom_credential(session, "FIRST_KEY")
    await repository.create_custom_credential(session, "SECOND_KEY")

    rows = await repository.list_custom_credentials(session)

    assert {r.key for r in rows} == {"FIRST_KEY", "SECOND_KEY"}


# --- observability.integrations.collect: custom credentials appear alongside the catalog ---


async def test_collect_lists_a_custom_credential_with_a_masked_preview(
    session: AsyncSession, tmp_path: Path
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("MY_CUSTOM_KEY=sk-live-abcd1234\n", encoding="utf-8")
    await repository.create_custom_credential(session, "MY_CUSTOM_KEY")
    await session.commit()
    settings = Settings(database_url=DATABASE_URL, env_file_path=env_path)

    snapshot = await integrations_metrics.collect(session, settings=settings)

    custom = next(c for c in snapshot.credentials if c.key == "MY_CUSTOM_KEY")
    assert custom.group == "Custom"
    assert custom.configured is True
    assert custom.preview == "••••1234"
    assert "sk-live" not in (custom.preview or "")


async def test_collect_reports_a_registered_but_unwritten_custom_key_as_unconfigured(
    session: AsyncSession, tmp_path: Path
) -> None:
    """Registered (in the DB) but no line for it in .env yet -- a real, if
    unusual, transient state (e.g. the write half of the request failed)."""
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    await repository.create_custom_credential(session, "MY_CUSTOM_KEY")
    await session.commit()
    settings = Settings(database_url=DATABASE_URL, env_file_path=env_path)

    snapshot = await integrations_metrics.collect(session, settings=settings)

    custom = next(c for c in snapshot.credentials if c.key == "MY_CUSTOM_KEY")
    assert custom.configured is False
    assert custom.preview is None


# --- POST /dashboard/integrations/credentials: the full endpoint ----------------------


async def test_create_custom_credential_endpoint_writes_and_registers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")
    monkeypatch.setenv("ENV_FILE_PATH", str(env_path))

    async with await _client() as client:
        response = await client.post(
            "/dashboard/integrations/credentials",
            json={"key": "my_weather_api_key", "value": "abc123"},
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "saved", "restart_required": True}
    assert env_path.read_text(encoding="utf-8") == "MY_WEATHER_API_KEY=abc123\n"


async def test_create_custom_credential_endpoint_rejects_a_key_already_in_the_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")
    monkeypatch.setenv("ENV_FILE_PATH", str(env_path))

    async with await _client() as client:
        response = await client.post(
            "/dashboard/integrations/credentials",
            json={"key": "hunter_api_key", "value": "abc123"},
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )

    assert response.status_code == 400
    assert env_path.read_text(encoding="utf-8") == ""


async def test_create_custom_credential_endpoint_rejects_a_reserved_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("DASHBOARD_AUTH_TOKEN", "correct-horse-battery-staple")
    monkeypatch.setenv("ENV_FILE_PATH", str(env_path))

    async with await _client() as client:
        response = await client.post(
            "/dashboard/integrations/credentials",
            json={"key": "PATH", "value": "/evil"},
            headers={"Authorization": "Bearer correct-horse-battery-staple"},
        )

    assert response.status_code == 400
    assert env_path.read_text(encoding="utf-8") == ""
