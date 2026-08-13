"""`/status` and `/issue #<tag>`: a human can ask the factory for a live
snapshot, or drill into one escalation's full detail, over Telegram instead
of only ever receiving push notifications.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.hitl.commands import build_issue_detail, build_status_report
from factory.hitl.interrupts import collect_replies
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import InterruptReason, ProductStatus, ScoredIdea

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


class _ScriptedNotifier:
    def __init__(self, replies: list[str]) -> None:
        self._replies = replies
        self.sent: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    async def poll_replies(self) -> list[str]:
        replies, self._replies = self._replies, []
        return replies


async def _make_product(
    session: AsyncSession, name: str, status: ProductStatus = ProductStatus.VALIDATING
) -> str:
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
    product_id = await repository.create_product(session, name=name, idea_id=idea_id)
    await repository.set_product_status(session, product_id, status)
    return product_id


# --- build_status_report -------------------------------------------------------------


async def test_reports_no_active_products_and_the_open_escalations_content(
    session: AsyncSession,
) -> None:
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject="STRIPE_API_KEY missing",
        detail="set it",
        headline="⚙️ Setup needed — STRIPE_API_KEY",
        reply_hint="No reply needed here — set the credential and restart the factory.",
        needs_reply=False,
    )
    assert interrupt_id is not None
    await session.flush()

    report = await build_status_report(session)

    assert "PRODUCTS — 0 active" in report
    assert "ESCALATIONS — 1 open" in report
    assert "issue: STRIPE_API_KEY missing" in report
    assert "Setup needed" in report  # the headline, not the raw "missing_credential"
    assert f"tag: #{interrupt_id[:8]}" in report
    assert "about: (not tied to a specific product)" in report


async def test_an_escalations_product_is_named_even_if_it_is_not_in_the_active_list(
    session: AsyncSession,
) -> None:
    """Regression: an escalation about a since-retired (or otherwise inactive)
    product used to render as a bare count next to whatever product *is*
    currently active, reading as though it belonged to that one instead."""
    stuck_product_id = await _make_product(session, "stuck-old-product")
    await repository.retire_product(session, stuck_product_id)
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.HEAL_EXHAUSTED,
        subject=f"stuck-old-product needs help ({stuck_product_id[:8]})",
        detail="Automated repair did not converge.",
        headline="\U0001f527 Repair stuck — needs your input",
        reply_hint="Reply with guidance for one more attempt, or REJECT to retire this product.",
        product_id=stuck_product_id,
    )
    assert interrupt_id is not None
    healthy_product_id = await _make_product(session, "healthy-current-product")
    await session.flush()

    report = await build_status_report(session)

    assert "healthy-current-product" in report
    # The escalation names its own (retired) product explicitly, distinct from
    # the currently active one listed above it.
    assert "about: stuck-old-product (retired)" in report
    assert healthy_product_id != stuck_product_id


async def test_an_escalations_product_name_is_never_truncated(session: AsyncSession) -> None:
    """Regression, found live: the escalation's product name used to be cut at 60
    chars. Real product names are `target_niche[:80]`, so two long, similarly-
    worded names (a realistic case: a reseeded idea reusing most of the original
    wording) had their one differentiating word land past that cutoff -- the
    escalation then showed a name that looked identical to whatever product was
    listed as active above it."""
    long_name = "Freelance bookkeepers managing 5+ small-business clients on QuickBooks or Xero"
    assert len(long_name) > 60  # the fixture must actually exercise the old cutoff
    stuck_product_id = await _make_product(session, long_name)
    await repository.retire_product(session, stuck_product_id)
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.HEAL_EXHAUSTED,
        subject="needs help",
        detail="Automated repair did not converge.",
        headline="\U0001f527 Repair stuck — needs your input",
        reply_hint="Reply with guidance for one more attempt, or REJECT to retire this product.",
        product_id=stuck_product_id,
    )
    assert interrupt_id is not None
    await session.flush()

    report = await build_status_report(session)

    assert f"about: {long_name} (retired)" in report


async def test_reports_a_products_name_stage_and_numbers(session: AsyncSession) -> None:
    product_id = await _make_product(session, "status-report-product")
    await repository.record_page_view(session, product_id=product_id, path="/")
    await repository.record_page_view(session, product_id=product_id, path="/")
    await repository.insert_validation_signal(
        session, product_id=product_id, email="a@example.test"
    )
    await session.flush()

    report = await build_status_report(session)

    assert "status-report-product" in report
    assert "validating" in report
    assert "views (7d): 2" in report
    assert "signals: 1" in report
    assert "leads: 0" in report


async def test_excludes_retired_products(session: AsyncSession) -> None:
    product_id = await _make_product(session, "status-report-retired")
    retired = await repository.retire_product(session, product_id)
    assert retired is True
    await session.flush()

    report = await build_status_report(session)

    assert "status-report-retired" not in report
    assert "PRODUCTS — 0 active" in report


async def test_old_page_views_fall_outside_the_seven_day_window(session: AsyncSession) -> None:
    from sqlalchemy import update

    from factory.state.orm import PageView

    product_id = await _make_product(session, "status-report-stale-views")
    await repository.record_page_view(session, product_id=product_id, path="/")
    await session.execute(
        update(PageView).values(created_at=datetime.now(UTC) - timedelta(days=30))
    )
    await session.flush()

    report = await build_status_report(session)

    assert "views (7d): 0" in report


# --- collect_replies dispatches /status without touching any interrupt --------------


async def test_collect_replies_answers_status_without_resolving_anything(
    session: AsyncSession,
) -> None:
    await _make_product(session, "status-dispatch-product")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject="unrelated escalation",
        detail="set it",
        headline="Test escalation",
        reply_hint="No reply needed here — set the credential and restart the factory.",
        needs_reply=False,
    )
    assert interrupt_id is not None
    await session.flush()

    notifier = _ScriptedNotifier(["/status"])
    resolved = await collect_replies(session, notifier)

    assert resolved == 0
    assert len(notifier.sent) == 1
    assert "status-dispatch-product" in notifier.sent[0]
    assert "ESCALATIONS — 1 open" in notifier.sent[0]
    assert "issue: unrelated escalation" in notifier.sent[0]

    # The unrelated interrupt must still be open -- /status must not resolve anything.
    still_open = await repository.stale_open_interrupts(session, older_than=timedelta(seconds=0))
    assert any(row.id == interrupt_id for row in still_open)


# --- build_issue_detail --------------------------------------------------------------


async def test_issue_detail_shows_the_full_text_status_only_summarises(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "issue-detail-product")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.HEAL_EXHAUSTED,
        subject="issue-detail-product needs help",
        detail=(
            "Automated repair did not converge. The last blocking feedback was:\n\n"
            "The test suite failed (exit 1): ModuleNotFoundError: No module named 'foo'"
        ),
        headline="\U0001f527 Repair stuck — needs your input",
        reply_hint="Reply with guidance for one more attempt, or REJECT to retire this product.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    await session.flush()

    detail = await build_issue_detail(session, interrupt_id[:8])

    assert "Repair stuck" in detail
    assert "issue-detail-product" in detail
    assert "ModuleNotFoundError: No module named 'foo'" in detail
    assert f"Include this tag in your reply: #{interrupt_id[:8]}" in detail


async def test_issue_detail_reports_a_tag_that_matches_nothing_open(
    session: AsyncSession,
) -> None:
    detail = await build_issue_detail(session, "deadbeef")

    assert "No open escalation matches #deadbeef" in detail


async def test_issue_detail_does_not_match_a_resolved_interrupt(session: AsyncSession) -> None:
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject="already handled",
        detail="the real question",
        headline="Test escalation",
        reply_hint="No reply needed here — set the credential and restart the factory.",
        needs_reply=False,
    )
    assert interrupt_id is not None
    await repository.resolve_interrupt(session, interrupt_id, "fixed")
    await session.flush()

    detail = await build_issue_detail(session, interrupt_id[:8])

    assert "No open escalation matches" in detail
    assert "the real question" not in detail


async def test_issue_detail_reflects_a_redaction_from_a_retired_product(
    session: AsyncSession,
) -> None:
    """The exact scenario found live: a product's interrupt detail is redacted
    the moment the product is retired (`purge_product_pii`) -- /issue must show
    whatever is actually left, not error out or fabricate the original text."""
    product_id = await _make_product(session, "issue-detail-retired-product")
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.HEAL_EXHAUSTED,
        subject="needs help",
        detail="The real original question, before redaction.",
        headline="\U0001f527 Repair stuck — needs your input",
        reply_hint="Reply with guidance for one more attempt, or REJECT to retire this product.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    await repository.purge_product_pii(session, product_id)
    await session.flush()

    detail = await build_issue_detail(session, interrupt_id[:8])

    assert "redacted" in detail
    assert "The real original question" not in detail


# --- collect_replies dispatches /issue ------------------------------------------------


async def test_collect_replies_answers_issue_without_resolving_it(session: AsyncSession) -> None:
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.MISSING_CREDENTIAL,
        subject="STRIPE_API_KEY missing",
        detail="Set STRIPE_API_KEY in the environment and reply retry.",
        headline="Test escalation",
        reply_hint="No reply needed here — set the credential and restart the factory.",
        needs_reply=False,
    )
    assert interrupt_id is not None
    await session.flush()

    notifier = _ScriptedNotifier([f"/issue #{interrupt_id[:8]}"])
    resolved = await collect_replies(session, notifier)

    assert resolved == 0
    assert len(notifier.sent) == 1
    assert "Set STRIPE_API_KEY in the environment and reply retry." in notifier.sent[0]

    still_open = await repository.stale_open_interrupts(session, older_than=timedelta(seconds=0))
    assert any(row.id == interrupt_id for row in still_open)
