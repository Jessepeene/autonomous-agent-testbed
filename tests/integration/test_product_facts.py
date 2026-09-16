"""Facts about a product, separate from where it sits on the conveyor.

`ProductStatus` cannot distinguish a product nobody has designed from one with
guidelines -- both are VALIDATING -- which is why the undesigned one went unnoticed
through a whole build. These assert the distinction the status enum cannot make.

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.facts import Unmet, product_facts
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import DesignGuidelines, ScoredIdea

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


def _scored_idea() -> ScoredIdea:
    return ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche="Bookkeepers at small Dutch accounting firms",
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month per seat, bought after the first close.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )


@dataclass
class _Signal:
    """The shape `record_idea_evidence` reads off a scout signal."""

    title: str = "Reconciliation is killing me"
    body: str = "Every month-end I lose two days to this."
    url: str = "https://example.test/comment"
    source: str = "test"
    engagement: int = 3


async def _product(session: AsyncSession) -> str:
    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    return await repository.create_product(session, name="Shardline", idea_id=idea)


async def test_a_new_product_is_missing_everything_and_says_so(session: AsyncSession) -> None:
    product_id = await _product(session)

    facts = await product_facts(session, product_id)
    assert facts is not None

    # Nothing has been decided about this product yet, and every one of those is a
    # separate absence rather than one undifferentiated "early".
    assert facts.unmet == (
        Unmet.AUDIENCE,
        Unmet.REQUIREMENTS,
        Unmet.DESIGN_GUIDELINES,
        Unmet.DEPLOYMENT,
        Unmet.REACH,
    )
    assert facts.audience_is_observed is False
    assert facts.is_deployed is False


async def test_the_audience_sentence_is_present_even_when_nothing_supports_it(
    session: AsyncSession,
) -> None:
    """The failure this fact exists to name.

    `design/work.py` reads `idea.target_niche` and calls it the audience. That sentence
    is always there, so a designer is never short of an answer -- it is just never an
    observed one. Present-but-inferred and present-but-researched have to be different
    states or nobody can tell which they are holding.
    """
    product_id = await _product(session)

    facts = await product_facts(session, product_id)
    assert facts is not None
    assert facts.audience_sentence == "Bookkeepers at small Dutch accounting firms"
    assert facts.audience_evidence == 0
    assert Unmet.AUDIENCE in facts.unmet


async def test_real_evidence_settles_the_audience(session: AsyncSession) -> None:
    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product_id = await repository.create_product(session, name="Shardline", idea_id=idea)
    await repository.record_idea_evidence(session, idea_id=idea, signals=[_Signal(), _Signal()])

    facts = await product_facts(session, product_id)
    assert facts is not None
    assert facts.audience_evidence == 2
    assert facts.audience_is_observed is True
    assert Unmet.AUDIENCE not in facts.unmet


async def test_design_guidelines_change_the_facts_without_changing_the_status(
    session: AsyncSession,
) -> None:
    """The distinction `ProductStatus` cannot express.

    Two products can sit in the same status and differ in whether anyone has decided
    how they should look. That difference is the entire reason one of them shipped a
    framework's default palette.
    """
    product_id = await _product(session)
    before = await product_facts(session, product_id)
    assert before is not None

    await repository.record_design_guidelines(
        session,
        product_id=product_id,
        guidelines=DesignGuidelines(
            direction="Borrow the language of ledgers: ruled columns, tabular figures.",
            palette=["#0b3d2e rule", "#f4f1ea paper", "#b8860b accent"],
            typography="IBM Plex Sans, with a monospace face for figures.",
            must_hold=["The page loads at least one webfont"],
        ),
    )

    after = await product_facts(session, product_id)
    assert after is not None
    assert after.status == before.status
    assert before.has_design_guidelines is False
    assert after.has_design_guidelines is True
    assert Unmet.DESIGN_GUIDELINES in before.unmet
    assert Unmet.DESIGN_GUIDELINES not in after.unmet


async def test_an_evidence_tally_its_own_rows_cannot_support_is_visible(
    session: AsyncSession,
) -> None:
    """A number that disagrees with what it counts.

    Ideas carry `evidence_count` from Discovery while `idea_evidence` holds what was
    actually kept. In production the first says 2 and the second is empty, so a decider
    told "2 pieces of evidence" cannot read any of them. Holding both is what makes the
    disagreement sayable at all.
    """
    from factory.handbook import product_brief
    from factory.state.orm import Idea

    idea = await repository.upsert_idea(session, _scored_idea())
    assert idea is not None
    product_id = await repository.create_product(session, name="Shardline", idea_id=idea)

    # Set the tally without storing anything for it to count -- which is exactly the
    # state every idea in production is in.
    row = await session.get(Idea, idea)
    assert row is not None
    row.evidence_count = 2
    await session.flush()

    facts = await product_facts(session, product_id)
    assert facts is not None
    assert facts.claimed_evidence_count == 2
    assert facts.audience_evidence == 0
    assert facts.evidence_count_is_trustworthy is False
    assert "CAUTION" in await product_brief(session, product_id)

    # ...and it stops complaining once the rows back the number up.
    await repository.record_idea_evidence(session, idea_id=idea, signals=[_Signal(), _Signal()])
    settled = await product_facts(session, product_id)
    assert settled is not None
    assert settled.evidence_count_is_trustworthy is True
    assert "CAUTION" not in await product_brief(session, product_id)


async def test_the_brief_is_a_rendering_of_the_facts(session: AsyncSession) -> None:
    """Prose and predicate must not drift: the sentence is generated from the fact."""
    from factory.handbook import product_brief

    product_id = await _product(session)
    brief = await product_brief(session, product_id)
    facts = await product_facts(session, product_id)
    assert facts is not None

    assert "NOT known:" in brief
    assert "nobody has established who this is for" in brief
    assert "nobody has written how it should look" in brief
    # Every unmet fact reaches the reader; none is silently dropped in rendering.
    assert brief.count(";") == len(facts.unmet) - 1


async def test_no_such_product_is_none_not_an_empty_product(session: AsyncSession) -> None:
    """Absent and empty are different answers, and a caller must be able to tell."""
    assert await product_facts(session, "0" * 32) is None
