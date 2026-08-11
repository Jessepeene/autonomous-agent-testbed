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
from factory.state.orm import Base, CampaignApproval, Interrupt, Lead, OutreachLog, Product
from factory.state.schemas import InterruptReason, ScoredIdea
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

    sent = await send_to_lead(ctx, session, lead, product_summary="A tool.", product_id=None)
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


# --- PII retention tied to product lifecycle (retire_product / purge_product_pii) --


async def _make_product(session: AsyncSession, name: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche=name,
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return await repository.create_product(session, name=name, idea_id=idea_id)


async def test_retiring_a_product_redacts_its_campaign_brief(session: AsyncSession) -> None:
    product_id = await _make_product(session, "retention-brief")
    task_id = await repository.enqueue_task(
        session,
        kind=repository.TaskKind.SEND_OUTREACH,
        engine=EngineName.MARKETING,
        idempotency_key=f"outreach:{product_id}",
        product_id=product_id,
    )
    assert task_id is not None
    approval = await repository.get_or_create_campaign_approval(
        session,
        product_id=product_id,
        task_id=task_id,
        lead_count=3,
        brief="Sample recipients: Head of Finance at Analytical Engines BV.",
    )
    await session.flush()

    retired = await repository.retire_product(session, product_id)

    assert retired is True
    row = await session.get(CampaignApproval, approval.id)
    assert row is not None
    assert "Analytical Engines" not in row.brief
    assert row.lead_count == 3  # audit-relevant fields survive the redaction


async def test_retiring_a_product_redacts_interrupts_linked_directly_and_via_task(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "retention-interrupts")

    direct_interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject="failed 3 heal cycles",
        detail="Automated repair did not converge. Contact role: CFO.",
        product_id=product_id,
    )
    assert direct_interrupt_id is not None

    task_id = await repository.enqueue_task(
        session,
        kind=repository.TaskKind.SEND_OUTREACH,
        engine=EngineName.MARKETING,
        idempotency_key=f"outreach:{product_id}:2",
        product_id=product_id,
    )
    assert task_id is not None
    via_task_interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.CAMPAIGN_APPROVAL,
        subject="campaign approval needed",
        detail="Sample recipients: Head of Finance at Analytical Engines BV.",
        task_id=task_id,
    )
    assert via_task_interrupt_id is not None
    await session.flush()

    await repository.retire_product(session, product_id)

    direct_row = await session.get(Interrupt, direct_interrupt_id)
    via_task_row = await session.get(Interrupt, via_task_interrupt_id)
    assert direct_row is not None and "CFO" not in direct_row.detail
    assert via_task_row is not None and "Analytical Engines" not in via_task_row.detail
    # subject/status/reason survive -- only the free-text detail is redacted
    assert direct_row.subject == "failed 3 heal cycles"


async def test_retiring_one_product_does_not_redact_a_different_products_interrupt(
    session: AsyncSession,
) -> None:
    product_a = await _make_product(session, "retention-a")
    product_b = await _make_product(session, "retention-b")

    interrupt_b = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject="product b escalation",
        detail="Product B detail must survive.",
        product_id=product_b,
    )
    assert interrupt_b is not None
    await session.flush()

    await repository.retire_product(session, product_a)

    row = await session.get(Interrupt, interrupt_b)
    assert row is not None
    assert row.detail == "Product B detail must survive."
    product_b_row = await session.get(Product, product_b)
    assert product_b_row is not None
    assert product_b_row.status != "retired"


async def test_purge_product_pii_is_idempotent(session: AsyncSession) -> None:
    product_id = await _make_product(session, "retention-idempotent")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject="failed 3 heal cycles",
        detail="Some detail.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    await session.flush()

    first = await repository.purge_product_pii(session, product_id)
    second = await repository.purge_product_pii(session, product_id)

    assert first == 1
    assert second == 0


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
