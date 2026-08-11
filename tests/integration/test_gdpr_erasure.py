"""GDPR Art. 17 erasure and the one-click unsubscribe link, end to end.

Regression coverage for two bugs found in the 2026-08-10 full-codebase audit, both
severe enough that no unsubscribe request had ever actually worked:

1. `send_to_lead` generated a token to embed in the outbound email's unsubscribe
   link, but `record_outreach` generated a *different*, unrelated token to store —
   so `GET /u/{token}` could never resolve a real recipient's link (404, always).
2. `erase_lead` deleted the `Lead` row while `outreach_log.lead_id` was a `NOT NULL`
   foreign key with no `ON DELETE` action -- any lead with outreach history (i.e.
   every lead who could actually click an unsubscribe link) crashed the erasure
   with a foreign-key violation instead of completing it.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.marketing.outreach import send_to_lead
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Lead, OutreachLog
from factory.supervisor.context import FactoryContext

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
    yield
    from factory.settings import get_settings
    from factory.state import db as db_module

    await db_module.dispose_engine()
    get_settings.cache_clear()


def _ctx(
    *, provider: FakeProvider, workspace_root: Path, blacklist_path: Path, **overrides: object
) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        blacklist_path=blacklist_path,
        outbound_from_address="factory@example.test",
        outbound_postal_address="Testlaan 1, 1000 AA Amsterdam, NL",
        unsubscribe_base_url="https://example.test",
        factory_dry_run=True,
        **overrides,  # type: ignore[arg-type]
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: provider}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.MARKETING},
        worker_id="gdpr-erasure-test",
    )


async def _make_lead(session: AsyncSession, *, email: str = "lead@example.test") -> Lead:
    lead = Lead(
        id="lead-" + email,
        first_name="Ada",
        last_name="Lovelace",
        job_title="Head of Operations",
        company="Analytical Engines BV",
        work_email=email,
        source_url="https://analytical-engines.test/team",
        scraped_at=datetime(2026, 8, 1, tzinfo=UTC),
        provenance_kind="direct",
        provider="hunter",
        lia_passed=True,
    )
    session.add(lead)
    await session.flush()
    return lead


def _draft_provider() -> FakeProvider:
    from factory.engines.marketing.outreach import OutreachDraft

    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        OutreachDraft,
        OutreachDraft(
            subject="A quicker way to reconcile invoices",
            body=(
                "Hi Ada, reconciling invoices by hand every week eats a full day. "
                "Our tool matches them automatically. Worth a five-minute look?"
            ),
        ),
    )
    return provider


# --- bug 1: the embedded token and the stored token must be the same value -----


async def test_sent_email_token_matches_the_stored_token(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    lead = await _make_lead(session)
    ctx = _ctx(provider=_draft_provider(), workspace_root=tmp_path, blacklist_path=blacklist_file)

    await send_to_lead(
        ctx, session, lead, product_summary="Automated invoice reconciliation.", product_id=None
    )
    # Must be committed, not just flushed: the /u/{token} endpoint below resolves
    # through a *different* connection (the process-wide session_scope() engine),
    # which cannot see an uncommitted row in this test's own transaction.
    await session.commit()

    row = (
        await session.execute(select(OutreachLog).where(OutreachLog.lead_id == lead.id))
    ).scalar_one()

    async with await _client(blacklist_path=blacklist_file) as client:
        response = await client.get(f"/u/{row.unsubscribe_token}")
    assert response.status_code == 200, response.text


async def test_blocked_send_also_stores_a_resolvable_token(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    """A content-policy-blocked draft never reaches a recipient, so its token is
    never load-bearing -- but it must still be internally consistent (no crash,
    no orphaned mismatch) since the same code path handles it."""
    lead = await _make_lead(session, email="blocked-case@example.test")
    provider = FakeProvider(name=Provider.GEMINI)
    from factory.engines.marketing.outreach import OutreachDraft

    provider.register(
        OutreachDraft,
        OutreachDraft(
            subject="Act now!!!",
            body=(
                "Click here now for a 100% free, risk-free offer with no obligation "
                "whatsoever, available today only!!"
            ),
        ),
    )
    ctx = _ctx(provider=provider, workspace_root=tmp_path, blacklist_path=blacklist_file)

    sent = await send_to_lead(
        ctx, session, lead, product_summary="A tool.", product_id=None
    )
    assert sent is False
    await session.flush()

    row = (
        await session.execute(select(OutreachLog).where(OutreachLog.lead_id == lead.id))
    ).scalar_one()
    assert row.status == "blocked"
    assert row.unsubscribe_token


# --- bug 2: erasure must not crash when the lead has outreach history ----------


async def test_erase_lead_does_not_raise_when_outreach_history_exists(
    session: AsyncSession,
) -> None:
    lead = await _make_lead(session, email="erase-me@example.test")
    for _ in range(3):
        await repository.record_outreach(
            session,
            lead_id=lead.id,
            subject="s",
            status="sent",
            unsubscribe_token="tok-" + lead.id + str(_),
        )
    await session.flush()

    await repository.erase_lead(session, lead.work_email)
    await session.flush()

    assert await session.get(Lead, lead.id) is None
    rows = (
        (await session.execute(select(OutreachLog).where(OutreachLog.product_id.is_(None))))
        .scalars()
        .all()
    )
    surviving = [r for r in rows if r.lead_id is None]
    assert len(surviving) == 3


async def test_unsubscribe_endpoint_completes_erasure_end_to_end(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    lead = await _make_lead(session, email="full-flow@example.test")
    ctx = _ctx(provider=_draft_provider(), workspace_root=tmp_path, blacklist_path=blacklist_file)

    await send_to_lead(
        ctx, session, lead, product_summary="Automated invoice reconciliation.", product_id=None
    )
    await session.commit()

    row = (
        await session.execute(select(OutreachLog).where(OutreachLog.lead_id == lead.id))
    ).scalar_one()
    token = row.unsubscribe_token

    async with await _client(blacklist_path=blacklist_file) as client:
        response = await client.get(f"/u/{token}")
    assert response.status_code == 200, response.text

    # The erasure happened through a *different* session (the endpoint's own
    # session_scope()) -- verify through an independent connection rather than
    # this test's own session, whose identity map/greenlet context is tied to
    # the fixtures set up earlier in this same test.
    verify_engine = create_async_engine(DATABASE_URL or "", future=True)
    verify_maker = async_sessionmaker(verify_engine, expire_on_commit=False)
    async with verify_maker() as verify_session:
        remaining_lead = (
            await verify_session.execute(select(Lead).where(Lead.id == lead.id))
        ).scalar_one_or_none()
        assert remaining_lead is None

        surviving = (
            await verify_session.execute(select(OutreachLog).where(OutreachLog.id == row.id))
        ).scalar_one()
        assert surviving.lead_id is None
    await verify_engine.dispose()


async def _client(*, blacklist_path: Path) -> httpx.AsyncClient:
    """The endpoint under test resolves its `Settings` through the module-global,
    `lru_cache`d `get_settings()` -- not through this file's `_ctx()` helper -- so
    env vars must be set *and the cache cleared* before each request, or a Settings
    object built (and frozen) by some earlier test file in the same pytest process
    silently wins. Without `blacklist_path` pointed at a test-scoped file, the
    endpoint's `erase_lead()` call would fall back to whatever `BLACKLIST_PATH`/
    default the real environment resolves to -- on a real deploy that's the actual
    production suppression list."""
    import os as _os

    from factory.settings import get_settings

    _os.environ["DATABASE_URL"] = DATABASE_URL or ""
    _os.environ.setdefault("GEMINI_API_KEY", "test-key")
    _os.environ["BLACKLIST_PATH"] = str(blacklist_path)
    get_settings.cache_clear()

    from factory.api.app import app

    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
