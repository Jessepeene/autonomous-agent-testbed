"""Reply-driven domain capture -- DOMAIN_REQUIRED's own branch in `collect_replies`.

This interrupt shape (`product_id` set, no `task_id`) is identical to the heal-
exhaustion escalation's own shape (see `test_hitl_product_guidance.py`), which
means "REJECT retires the product" for that one and "capture a hostname" for
this one -- the two must never cross-talk. `reason` is what disambiguates them;
this file also re-covers the heal-exhaustion path is untouched implicitly by
using a different `InterruptReason` throughout.

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

from factory.hitl.interrupts import collect_replies
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


async def _make_live_product(session: AsyncSession, niche: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche=niche,
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name=niche, idea_id=idea_id)
    await repository.set_product_status(session, product_id, ProductStatus.LIVE)
    return product_id


async def _domain_requested_interrupt(session: AsyncSession, product_id: str, subject: str) -> str:
    """Mirrors development/domain.py::ensure_requested's real escalation shape."""
    interrupt_id = await repository.open_interrupt(
        session,
        reason=InterruptReason.DOMAIN_REQUIRED,
        subject=subject,
        detail="Reply with a domain you own, or SKIP.",
        product_id=product_id,
    )
    assert interrupt_id is not None
    await repository.set_domain_status(session, product_id, "pending")
    return interrupt_id


async def test_a_valid_hostname_reply_captures_it_and_queues_attachment(
    session: AsyncSession,
) -> None:
    product_id = await _make_live_product(session, "domain-valid")
    interrupt_id = await _domain_requested_interrupt(session, product_id, "Domain for X")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} example.com"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.custom_domain == "example.com"
    assert product.domain_status == "pending"

    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.ATTACH_DOMAIN.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(tasks) == 1
    assert tasks[0].payload["hostname"] == "example.com"


async def test_a_url_shaped_reply_is_normalised_to_a_bare_hostname(
    session: AsyncSession,
) -> None:
    product_id = await _make_live_product(session, "domain-url-shaped")
    interrupt_id = await _domain_requested_interrupt(session, product_id, "Domain for Y")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} https://www.example.com/"])

    await collect_replies(session, notifier)

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.custom_domain == "www.example.com"


async def test_a_skip_reply_marks_the_product_skipped_without_queuing_anything(
    session: AsyncSession,
) -> None:
    product_id = await _make_live_product(session, "domain-skip")
    interrupt_id = await _domain_requested_interrupt(session, product_id, "Domain for Z")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} SKIP, happy with the vercel url"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.domain_status == "skipped"
    assert product.custom_domain is None

    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.ATTACH_DOMAIN.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert tasks == []


async def test_an_empty_reply_is_treated_as_skip(session: AsyncSession) -> None:
    product_id = await _make_live_product(session, "domain-empty")
    interrupt_id = await _domain_requested_interrupt(session, product_id, "Domain for W")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]}"])

    await collect_replies(session, notifier)

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.domain_status == "skipped"


async def test_an_unparseable_reply_changes_nothing(session: AsyncSession) -> None:
    """Matches this module's existing convention for every other malformed-reply
    case (see _act_on_idea_reply/_act_on_product_reply): no automatic re-prompt,
    the interrupt is simply marked resolved with no further action."""
    product_id = await _make_live_product(session, "domain-invalid")
    interrupt_id = await _domain_requested_interrupt(session, product_id, "Domain for V")
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} not a real domain at all!!"])

    resolved = await collect_replies(session, notifier)

    assert resolved == 1
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.custom_domain is None
    assert product.domain_status == "pending"

    tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.product_id == product_id, Task.kind == TaskKind.ATTACH_DOMAIN.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert tasks == []


async def test_domain_reply_never_retires_the_product(session: AsyncSession) -> None:
    """The exact regression this branch exists to prevent: a DOMAIN_REQUIRED
    interrupt shares (product_id set, task_id unset) with a heal-exhaustion
    escalation, whose own REJECT handling would retire the product outright."""
    product_id = await _make_live_product(session, "domain-reject-word")
    interrupt_id = await _domain_requested_interrupt(session, product_id, "Domain for U")
    # "REJECT" is meaningless vocabulary for a domain reply, but it must not be
    # accidentally interpreted through the heal-exhaustion branch's lens either.
    notifier = _ScriptedNotifier([f"#{interrupt_id[:8]} reject, not now"])

    await collect_replies(session, notifier)

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.status == ProductStatus.LIVE.value
