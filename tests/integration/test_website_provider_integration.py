"""DB-dependent behavior for the web-search lead provider that unit tests can't
exercise: `has_lead_for_company`/`recently_processed_urls` (real `leads` table
queries) and `build_qualification_brief`'s generate-once-then-reuse caching
(real `Product.lead_qualification_brief` column, migration 0018).

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

from factory.engines.marketing.leads import build_qualification_brief
from factory.engines.marketing.providers.base import QualificationBrief
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import ScoredIdea
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


def _ctx(gemini: FakeProvider) -> FactoryContext:
    settings = Settings(database_url=DATABASE_URL, gemini_api_key="test-key")
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: gemini}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.MARKETING},
        worker_id="website-provider-integration-test",
    )


async def _make_product(session: AsyncSession, name: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche=name,
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month, bought after the first month-end close.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    return await repository.create_product(session, name=name, idea_id=idea_id)


async def _insert_lead(
    session: AsyncSession,
    *,
    first_name: str = "Ada",
    last_name: str = "Lovelace",
    company: str = "Acme Books Inc.",
    work_email: str = "ada@acme-books.test",
    source_url: str = "https://acme-books.test/team",
    scraped_at: datetime | None = None,
) -> None:
    await repository.insert_lead(
        session,
        {
            "first_name": first_name,
            "last_name": last_name,
            "job_title": "Owner",
            "company": company,
            "work_email": work_email,
            "source_url": source_url,
            "scraped_at": scraped_at or datetime.now(UTC),
        },
        lia_passed=True,
    )


# --- has_lead_for_company ----------------------------------------------------------------


async def test_has_lead_for_company_matches_despite_punctuation_and_suffix_drift(
    session: AsyncSession,
) -> None:
    """Production bug this guards against: "Acme Books, Inc." and "Acme Books
    Inc." must be recognised as the same company, not two different ones."""
    await _insert_lead(session, company="Acme Books, Inc.")
    await session.flush()

    assert (
        await repository.has_lead_for_company(
            session, company="Acme Books Inc.", name="Ada Lovelace"
        )
        is True
    )


async def test_has_lead_for_company_is_false_for_a_genuinely_different_company(
    session: AsyncSession,
) -> None:
    await _insert_lead(session, company="Acme Books Inc.")
    await session.flush()

    assert await repository.has_lead_for_company(session, company="Other Company LLC") is False


async def test_has_lead_for_company_without_a_name_matches_on_company_alone(
    session: AsyncSession,
) -> None:
    await _insert_lead(session, company="Acme Books Inc.")
    await session.flush()

    assert await repository.has_lead_for_company(session, company="Acme Books Inc.") is True


# --- recently_processed_urls --------------------------------------------------------------


async def test_recently_processed_urls_includes_a_recent_scrape(session: AsyncSession) -> None:
    await _insert_lead(session, source_url="https://acme-books.test/team")
    await session.flush()

    urls = await repository.recently_processed_urls(
        session, since=datetime.now(UTC) - timedelta(days=7)
    )

    assert "https://acme-books.test/team" in urls


async def test_recently_processed_urls_excludes_an_old_scrape(session: AsyncSession) -> None:
    await _insert_lead(
        session,
        source_url="https://acme-books.test/team",
        scraped_at=datetime.now(UTC) - timedelta(days=30),
    )
    await session.flush()

    urls = await repository.recently_processed_urls(
        session, since=datetime.now(UTC) - timedelta(days=7)
    )

    assert "https://acme-books.test/team" not in urls


# --- build_qualification_brief: generate once, cache, reuse ------------------------------


async def test_qualification_brief_is_generated_once_and_reused(session: AsyncSession) -> None:
    """The real regression this guards: regenerating the brief on every harvest
    would waste an LLM call on something that doesn't change call to call. Only
    one `QualificationBrief` is ever queued below -- if the second call tried to
    regenerate, `FakeProvider` would raise, failing this test."""
    product_id = await _make_product(session, "brief-cache-product")
    await session.flush()

    gemini = FakeProvider()
    gemini.register(
        QualificationBrief,
        QualificationBrief(
            positive_criteria=["Must be a freelance bookkeeper"],
            strict_exclusions=["Never extract an in-house employee"],
            borderline_guidance="When unsure, skip.",
        ),
    )
    ctx = _ctx(gemini)

    first = await build_qualification_brief(
        ctx, session, product_id=product_id, product_summary="A tool for freelance bookkeepers."
    )
    await session.commit()
    assert first is not None
    assert first.positive_criteria == ["Must be a freelance bookkeeper"]

    second = await build_qualification_brief(
        ctx, session, product_id=product_id, product_summary="A tool for freelance bookkeepers."
    )

    assert second == first


async def test_qualification_brief_generation_failure_returns_none(
    session: AsyncSession,
) -> None:
    from factory.llm.providers.base import LLMError

    product_id = await _make_product(session, "brief-failure-product")
    await session.flush()

    gemini = FakeProvider()
    gemini.register(QualificationBrief, LLMError(Provider.GEMINI, "schema violation"))
    ctx = _ctx(gemini)

    result = await build_qualification_brief(
        ctx, session, product_id=product_id, product_summary="A tool for freelance bookkeepers."
    )

    assert result is None
