"""Reply-driven DEFER for a heal-exhausted product.

Before this, a reply to a heal-exhaustion escalation could only retire the
product (REJECT) or be treated as guidance for another patch attempt (anything
else) -- there was no way for the actual product owner to say "ship it with
this known issue, I'll accept the risk" without their reply being misread as
technical instructions for the repair agent. See
`hitl/interrupts.py::_DEFER_WORD` and `development/graph.py::deploy_accepting_risk`.

Needs Postgres for the JSONB task payloads:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.development import graph as development_graph
from factory.hitl.interrupts import collect_replies
from factory.memory.store import recall_block, scopes_for
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Product, Task
from factory.state.schemas import InterruptReason, ProductStatus, ScoredIdea, TaskKind

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


@pytest.fixture(autouse=True)
def _dry_run_deploys(monkeypatch: pytest.MonkeyPatch) -> None:
    """`deploy_accepting_risk` calls the real `get_settings()` when no override is
    threaded through the reply chain (there is nowhere for a per-request Settings
    to travel through Telegram/dashboard replies) -- pin it to a hermetic, dry-run
    Settings instance so this test never depends on whatever `.env`/environment
    the test process happens to see, and never attempts a real Vercel call."""

    def _settings() -> Settings:
        return Settings(factory_dry_run=True)

    monkeypatch.setattr(development_graph, "get_settings", _settings)


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
    product_id = await repository.create_product(session, name=name, idea_id=idea_id)
    # A heal-exhausted product always carries a workspace and a nonzero
    # heal_attempts count by the time a human sees it -- mirror that real shape
    # rather than the fresh-product default, since deploy_accepting_risk resets
    # heal_attempts and that reset is one of the things under test.
    await repository.set_product_status(
        session,
        product_id,
        ProductStatus.PLANNED,
        workspace_path=f"product-{product_id[:12]}",
        heal_attempts=3,
    )
    return product_id


async def _heal_exhausted_interrupt(session: AsyncSession, product_id: str, subject: str) -> str:
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.LEGAL_UNCERTAINTY,
        subject=subject,
        detail="Automated repair did not converge; security and performance issues remain.",
        headline="\U0001f527 Repair stuck — needs your input",
        reply_hint=(
            "Reply with guidance for one more attempt, DEFER to deploy as-is and "
            "accept this as a known, un-fixed risk, or REJECT to retire this product."
        ),
        product_id=product_id,
    )
    assert interrupt_id is not None
    return interrupt_id


async def test_defer_reply_deploys_instead_of_queuing_a_patch(session: AsyncSession) -> None:
    product_id = await _make_product(session, "invoice-bot-defer")
    interrupt_id = await _heal_exhausted_interrupt(session, product_id, "failed 3 heal cycles")
    answer = "DEFER -- leave the vulnerability open for the mvp stage, note it for later"
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} {answer}"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.LIVE.value
    assert product.deploy_url is not None and product.deploy_url.startswith("dry-run://")
    assert product.heal_attempts == 0

    patch_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.PATCH_PRODUCT.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert patch_tasks == []  # the whole bug this fix closes: no guidance-as-patch


async def test_defer_reply_still_enqueues_lead_harvesting(session: AsyncSession) -> None:
    product_id = await _make_product(session, "invoice-bot-defer-leads")
    interrupt_id = await _heal_exhausted_interrupt(session, product_id, "failed 3 heal cycles")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} defer this, ship it now"])

    assert await collect_replies(session, notifier) == 1

    harvest_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.HARVEST_LEADS.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(harvest_tasks) == 1


async def test_defer_reply_records_the_accepted_risk_as_a_durable_note(
    session: AsyncSession,
) -> None:
    product_id = await _make_product(session, "invoice-bot-defer-note")
    interrupt_id = await _heal_exhausted_interrupt(session, product_id, "failed 3 heal cycles")
    answer = "defer -- leave the checkout race condition, fix before real production traffic"
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} {answer}"])

    assert await collect_replies(session, notifier) == 1

    recalled = await recall_block(
        session,
        query="checkout race condition",
        scopes=scopes_for(engine=EngineName.DEVELOPMENT, product_id=product_id),
    )
    assert "checkout race condition" in recalled


async def test_reject_wins_over_a_reply_that_also_contains_defer(session: AsyncSession) -> None:
    """`_REJECT_WORD` is checked first in `_act_on_product_reply` -- an operator
    typing "reject" must never be second-guessed by a coincidental "defer"
    elsewhere in the same message."""
    product_id = await _make_product(session, "invoice-bot-reject-wins")
    interrupt_id = await _heal_exhausted_interrupt(session, product_id, "failed 3 heal cycles")
    notifier = _ScriptedNotifier(
        [f"#{interrupt_id[:8]} REJECT -- we already deferred this idea once before, not worth it"]
    )

    assert await collect_replies(session, notifier) == 1

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.RETIRED.value
