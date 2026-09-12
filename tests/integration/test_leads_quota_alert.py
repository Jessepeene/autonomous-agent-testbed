"""A metered lead-provider quota hit (HTTP 429, or the internal rolling
30-day cap) must surface as an `IntegrationAlert` on the dashboard's
Integrations page, not an `Interrupt` -- nothing here needs a human decision
(see `marketing/providers/base.py::ProviderQuotaExceeded`'s docstring), only
visibility, and it must not be mistaken for a `credential_problem`/
`request_problem` (which do trigger an immediate escalation -- see
test_lead_sourcing_escalation.py).

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.marketing import leads as leads_module
from factory.engines.marketing.providers.base import LeadSearchPlan, ProviderQuotaExceeded
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state import db as db_module
from factory.state import repository
from factory.state.orm import Base
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
    await db_module.dispose_engine()


def _ctx(*, workspace_root: Path, lead_provider_monthly_quota: int = 500) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        lead_provider_monthly_quota=lead_provider_monthly_quota,
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: FakeProvider()}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.MARKETING},
        worker_id="leads-quota-alert-test",
    )


class _QuotaExceededProvider:
    name = "hunter.io"

    @property
    def available(self) -> bool:
        return True

    async def search(self, plan: LeadSearchPlan, *, limit: int = 25) -> list[object]:
        raise ProviderQuotaExceeded("hunter.io", "reached the limit for the number of searches")


async def test_a_provider_429_raises_an_integration_alert_not_an_interrupt(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planning/refinement (both real LLM calls -- one non-agentic, one an
    agent that tests tools) are stubbed out here on purpose: this test is
    about what happens once a plan already exists and a real provider is
    queried, not about the planner itself, which has its own coverage
    elsewhere. Mirrors `test_lead_sourcing_escalation.py`'s own use of
    `monkeypatch.setattr(leads_module, "ingest", ...)`, just one level
    shallower so the real provider loop -- the thing actually under test --
    still runs for real."""
    draft = LeadSearchPlan(
        job_titles=["Operations Manager"],
        company_domains=["example.test"],
        rationale="Operations managers approve recurring software spend.",
    )

    async def fake_plan_search(*args: object, **kwargs: object) -> tuple[LeadSearchPlan, str]:
        return draft, "test-planner-version"

    async def fake_refine(*args: object, **kwargs: object) -> LeadSearchPlan:
        return draft

    async def fake_real_adapters(*args: object, **kwargs: object) -> list[object]:
        return [_QuotaExceededProvider()]

    monkeypatch.setattr(leads_module, "plan_search", fake_plan_search)
    monkeypatch.setattr(leads_module, "refine_sourcing_strategy", fake_refine)
    monkeypatch.setattr(leads_module, "_real_adapters", fake_real_adapters)

    ctx = _ctx(workspace_root=tmp_path)
    report = await leads_module.ingest(ctx, session, product_summary="a test product")
    await session.commit()

    assert report.credential_problem is None
    assert report.request_problem is None
    assert await repository.count_open_interrupts(session) == 0
    alerts = await repository.list_open_integration_alerts(session)
    assert len(alerts) == 1
    assert alerts[0].provider == "hunter.io"
    assert "reached the limit" in alerts[0].message


async def test_the_internal_monthly_quota_reached_also_raises_an_alert(
    session: AsyncSession, tmp_path: Path
) -> None:
    ctx = _ctx(workspace_root=tmp_path, lead_provider_monthly_quota=0)

    report = await leads_module.ingest(ctx, session, product_summary="a test product")
    await session.commit()

    assert report.quota_blocked is True
    alerts = await repository.list_open_integration_alerts(session)
    assert len(alerts) == 1
    assert alerts[0].provider == "lead_provider_monthly_quota"
