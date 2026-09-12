"""The holistic tone self-check on outreach copy (Tier 0 of the system-wide
verification plan) -- `compose()`'s own draft can be factually compliant
(passes `check_outreach_content`'s deterministic disclosure checks) while
still reading generic, robotic, or like an uncustomized template. See
`outreach.py::OutreachDraft.reads_like_a_real_person_wrote_it`.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.marketing import email as email_module
from factory.engines.marketing.outreach import OutreachDraft, send_to_lead
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
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


def _ctx(*, provider: FakeProvider, workspace_root: Path, blacklist_path: Path) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        blacklist_path=blacklist_path,
        outbound_from_address="factory@example.test",
        outbound_postal_address="Testlaan 1, 1000 AA Amsterdam, NL",
        unsubscribe_base_url="https://example.test",
        factory_dry_run=True,
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: provider}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.MARKETING},
        worker_id="outreach-tone-check-test",
    )


async def _make_lead(session: AsyncSession) -> Lead:
    lead = Lead(
        id="lead-tone-check",
        first_name="Ada",
        last_name="Lovelace",
        job_title="Head of Operations",
        company="Analytical Engines BV",
        work_email="ada@example.test",
        source_url="https://analytical-engines.test/team",
        scraped_at=datetime(2026, 8, 1, tzinfo=UTC),
        provenance_kind="direct",
        provider="hunter",
        lia_passed=True,
    )
    session.add(lead)
    await session.flush()
    return lead


async def test_a_flagged_tone_check_blocks_the_send_even_with_compliant_content(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    """The deterministic checks in `check_outreach_content` only look at
    disclosure/spam phrases -- a draft with no spam phrases, a real
    unsubscribe link, and a real postal address still must not ship if the
    model's own holistic self-check flagged it."""
    lead = await _make_lead(session)
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        OutreachDraft,
        OutreachDraft(
            subject="Reconciliation software for your team",
            body=(
                "Dear Sir/Madam, we are pleased to introduce our innovative solution "
                "for your business needs. Our platform offers many features. Please "
                "let us know if interested."
            ),
            reads_like_a_real_person_wrote_it=False,
            tone_concern="Generic template greeting, never references the recipient's "
            "actual role or company by name.",
        ),
    )
    ctx = _ctx(provider=provider, workspace_root=tmp_path, blacklist_path=blacklist_file)

    sent = await send_to_lead(
        ctx, session, lead, product_summary="Automated invoice reconciliation.", product_id=None
    )

    assert sent is False
    await session.flush()
    row = (
        await session.execute(select(OutreachLog).where(OutreachLog.lead_id == lead.id))
    ).scalar_one()
    assert row.status == "blocked"
    assert "tone" in (row.suppressed_reason or "").lower()


async def test_a_passed_tone_check_does_not_block_an_otherwise_compliant_send(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    lead = await _make_lead(session)
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        OutreachDraft,
        OutreachDraft(
            subject="A quicker way to reconcile invoices",
            body=(
                "Hi Ada, reconciling invoices by hand every week eats a full day. "
                "Our tool matches them automatically. Worth a five-minute look?"
            ),
            reads_like_a_real_person_wrote_it=True,
        ),
    )
    ctx = _ctx(provider=provider, workspace_root=tmp_path, blacklist_path=blacklist_file)

    sent = await send_to_lead(
        ctx, session, lead, product_summary="Automated invoice reconciliation.", product_id=None
    )

    # `_ctx` sets `factory_dry_run=True` (never transmit for real in a test), so
    # `send_to_lead` still returns False here via `send_email`'s own dry-run
    # gate -- that is a *different* False than the tone check's, and the
    # distinction is exactly what this test needs to prove. Checking the
    # persisted row's status/reason (below) is what actually shows the tone
    # check let this one through.
    assert sent is False
    await session.flush()
    row = (
        await session.execute(select(OutreachLog).where(OutreachLog.lead_id == lead.id))
    ).scalar_one()
    assert row.status == "suppressed"
    assert "tone" not in (row.suppressed_reason or "").lower()
    assert "dry_run" in (row.suppressed_reason or "").lower()


async def test_a_real_send_composes_the_learned_identity_and_records_its_version(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    """With dry-run off and Mailgun mocked, a send that actually goes out must
    carry the sender identity's display name in `from` and stamp
    `sender_identity_version_id` -- the one thing a *blocked* or *suppressed* send
    (the two tests above) must never do, since neither had a chance to convert."""
    lead = await _make_lead(session)
    provider = FakeProvider(name=Provider.GEMINI)
    provider.register(
        OutreachDraft,
        OutreachDraft(
            subject="A quicker way to reconcile invoices",
            body=(
                "Hi Ada, reconciling invoices by hand every week eats a full day. "
                "Our tool matches them automatically. Worth a five-minute look?"
            ),
            reads_like_a_real_person_wrote_it=True,
        ),
    )
    base_ctx = _ctx(provider=provider, workspace_root=tmp_path, blacklist_path=blacklist_file)
    live_settings = base_ctx.settings.model_copy(
        update={
            "mailgun_api_key": SecretStr("key-test"),
            "mailgun_domain": "mail.manifestaudit.com",
            "factory_dry_run": False,
        }
    )
    ctx = FactoryContext(
        settings=live_settings,
        llm=LLMClient(ProviderRegistry({Provider.GEMINI: provider}), settings=live_settings),
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.MARKETING},
        worker_id="outreach-tone-check-test-live",
    )

    with patch.object(email_module, "_mailgun_send") as transport:
        sent = await send_to_lead(
            ctx, session, lead, product_summary="Automated invoice reconciliation.", product_id=None
        )

    assert sent is True
    transport.assert_called_once()
    payload = transport.call_args.args[0]
    assert payload["from"] == "Alex Reyes <factory@example.test>"

    await session.flush()
    row = (
        await session.execute(select(OutreachLog).where(OutreachLog.lead_id == lead.id))
    ).scalar_one()
    assert row.status == "sent"
    assert row.sender_identity_version_id is not None
