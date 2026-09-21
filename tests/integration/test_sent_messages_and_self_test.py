"""What the company says is kept, and a self-test signal is one that does not count.

Both are removals of a narrowing rather than new rules. BUG_LOG 36 stopped a
reviewer's invented signup inflating the build-or-scrap count by making its reserved
address leave no trace at all -- and a submission that records nothing composes no
confirmation email and issues no token, so everything past the form became invisible
to review by construction.

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

from factory.state import repository
from factory.state.orm import Base, SentMessage
from factory.state.schemas import ScoredIdea

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
    """`read_sent_messages` opens its own `session_scope`, which uses the process-wide
    cached engine -- a different connection from this file's `session` fixture. Without
    disposing it around each test, a pooled connection outlives the `drop_all` another
    test's fixture runs, and this passes alone but fails in the suite. Same fixture and
    same reason as `test_validation_gate.py`'s."""
    yield
    from factory.state import db as db_module

    await db_module.dispose_engine()


async def _product(session: AsyncSession) -> str:
    idea = await repository.upsert_idea(
        session,
        ScoredIdea(
            problem_statement="Bookkeepers reconcile invoices by hand every week.",
            target_niche="Bookkeepers at small Dutch accounting firms",
            source_url="https://example.test/thread",
            monetization_hypothesis="EUR 49/month per seat.",
            pain_severity=0.8,
            willingness_to_pay=0.7,
            build_feasibility=0.6,
            adoption_ease=0.8,
        ),
    )
    assert idea is not None
    return await repository.create_product(session, name="Shardline", idea_id=idea)


async def test_a_self_test_signal_is_recorded_but_never_counted(session: AsyncSession) -> None:
    """Both halves. Recorded, so the journey past the form can be reviewed at all;
    excluded, so a build-or-scrap decision never counts the company testing itself."""
    product_id = await _product(session)

    await repository.insert_validation_signal(
        session,
        product_id=product_id,
        email="qa@factory-selftest.invalid",
        require_confirmation=False,
        is_self_test=True,
    )
    await repository.insert_validation_signal(
        session, product_id=product_id, email="baker@example.test", require_confirmation=False
    )
    await session.flush()

    assert await repository.tally_validation_signals(session, product_id) == 1
    rows = (await session.execute(select(repository.ValidationSignal))).scalars().all()
    assert len(rows) == 2, "the self-test signal must exist, it just must not count"
    assert {r.is_self_test for r in rows} == {True, False}


async def test_a_self_test_submission_gets_a_real_confirmation_token(
    session: AsyncSession,
) -> None:
    """The dead end this removes: no token meant no confirmation page to review."""
    product_id = await _product(session)

    signal_id = await repository.insert_validation_signal(
        session,
        product_id=product_id,
        email="qa@factory-selftest.invalid",
        require_confirmation=True,
        confirmation_token="tok-selftest-000",
        is_self_test=True,
    )
    await session.flush()

    assert signal_id is not None
    row = (await session.execute(select(repository.ValidationSignal))).scalars().one()
    assert row.confirmation_token == "tok-selftest-000"
    # ...and it confirms like any other, so the reviewer can follow the real link.
    assert await repository.confirm_validation_signal(session, "tok-selftest-000") is not None


async def test_an_unroutable_recipient_is_composed_and_kept_but_never_sent(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RFC 2606 domains can only hard-bounce, so the message is written and retained
    rather than handed to a transport. Stated as a fact about the transport, not a
    rule about the reviewer -- any unroutable address behaves the same."""
    from factory.engines.marketing import email as email_mod

    sent: list[object] = []
    monkeypatch.setattr(email_mod, "_mailgun_send", lambda *a, **k: sent.append(a))

    result = await email_mod.send_email(
        to="qa@factory-selftest.invalid",
        subject="Please confirm your interest",
        body="Thanks for your interest — please confirm.",
        product_id="prod-1",
    )

    assert result.sent is False
    assert "RFC 2606" in result.reason
    assert sent == [], "an unroutable address must never reach a transport"


async def test_erasure_deletes_what_was_written_to_that_person(session: AsyncSession) -> None:
    """A retained message holds what was said *to them*. Erasure that stops at the
    signal row leaves their data behind under a different table name."""
    from factory.state.orm import new_id

    session.add(
        SentMessage(
            id=new_id(),
            product_id=None,
            to_address="baker@example.test",
            from_address="alex@factory.test",
            subject="Please confirm your interest",
            body="Thanks for your interest.",
            outcome="sent",
        )
    )
    session.add(
        SentMessage(
            id=new_id(),
            product_id=None,
            to_address="someone.else@example.test",
            from_address="alex@factory.test",
            subject="Please confirm your interest",
            body="Thanks for your interest.",
            outcome="sent",
        )
    )
    await session.flush()

    await repository.erase_validation_signals(session, "baker@example.test")
    await session.flush()

    remaining = (await session.execute(select(SentMessage.to_address))).scalars().all()
    assert remaining == ["someone.else@example.test"]


async def test_a_reviewer_can_read_the_confirmation_and_find_its_link(
    session: AsyncSession,
) -> None:
    """The third of the three, and the one that was missing.

    Retaining the message and widening the origin allowlist are both useless while
    nothing can read the mail: the confirmation link exists only inside it, so a
    reviewer that submits the form has no way to learn the token and the journey
    stops at the form regardless.
    """
    from factory.state.orm import new_id
    from factory.tools.messages import confirmation_links, register_message_tools
    from factory.tools.registry import ToolRegistry

    product_id = await _product(session)
    session.add(
        SentMessage(
            id=new_id(),
            product_id=product_id,
            to_address="preship-review@factory-selftest.invalid",
            from_address="alex@factory.test",
            subject="Please confirm your interest",
            body="Thanks for your interest.\n\nhttps://api.example.test/confirm/tok-123",
            outcome="composed_only",
            detail="recipient domain cannot receive mail (RFC 2606)",
        )
    )
    await session.commit()

    registry = ToolRegistry()
    register_message_tools(registry, product_id=product_id)
    result = await registry.get("read_sent_messages").handler()

    assert result["count"] == 1
    message = result["messages"][0]
    assert message["subject"] == "Please confirm your interest"
    assert message["outcome"] == "composed_only"
    # The link a visitor would click, reachable from what the reviewer can read.
    assert confirmation_links(message["body"]) == ["https://api.example.test/confirm/tok-123"]
    # ...and the recipient is not handed to the transcript.
    assert message["to"] == "<recipient>@factory-selftest.invalid"
