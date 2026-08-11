"""Custom domain lifecycle end to end: request on first LIVE deploy, attach,
verify.

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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.development import domain as domain_module
from factory.engines.development import graph as dev_graph
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.sandbox.runner import DevOpsError, SandboxResult
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Interrupt, Product, Task, new_id
from factory.state.schemas import (
    CouncilResult,
    InterruptReason,
    ProductStatus,
    ScoredIdea,
    TaskKind,
    TaskStatus,
)
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
    from factory.state import db as db_module

    await db_module.dispose_engine()


class _PassingSandbox:
    """Available, and every command succeeds -- these tests are about the
    domain-request side effect of a successful deploy, not the sandbox itself."""

    available = True

    async def execute(self, command: str, *, workspace: str, **kwargs: object) -> SandboxResult:
        if "pip install" in command:
            return SandboxResult(exit_code=0, stdout="installed", stderr="")
        return SandboxResult(exit_code=0, stdout="1 passed", stderr="")


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, text: str) -> bool:
        self.sent.append(text)
        return True

    async def poll_replies(self) -> list[str]:
        return []


def _ctx(
    *, workspace_root: Path, notifier: _RecordingNotifier, **overrides: object
) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        **overrides,
    )
    llm = LLMClient(
        ProviderRegistry({Provider.GEMINI: FakeProvider(name=Provider.GEMINI)}), settings=settings
    )
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=notifier,
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT},
        worker_id="domain-lifecycle-test",
    )


async def _make_live_product(
    session: AsyncSession, niche: str, *, vercel_project: str | None = "proj-1"
) -> str:
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
    await repository.set_product_status(
        session, product_id, ProductStatus.LIVE, vercel_project=vercel_project
    )
    return product_id


# --- _verify_and_ship: requesting a domain on first LIVE ----------------------------


async def test_a_products_first_live_deploy_requests_a_domain(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.engines.development import bug_council
    from factory.engines.development import deploy as deploy_module

    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche="domain-first-live",
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name="Domain First", idea_id=idea_id)
    await session.flush()

    async def fake_convene(*args: object, **kwargs: object) -> CouncilResult:
        return CouncilResult(approved=True, verdicts=[])

    async def fake_deploy(*args: object, **kwargs: object) -> object:
        return deploy_module.DeployResult(
            ok=True, url="https://domain-first.vercel.app", detail="ok", project="domain-first"
        )

    monkeypatch.setattr(bug_council, "convene", fake_convene)
    monkeypatch.setattr(dev_graph.deploy_module, "deploy", fake_deploy)

    notifier = _RecordingNotifier()
    ctx = _ctx(workspace_root=tmp_path, notifier=notifier)
    ctx.sandbox = _PassingSandbox()  # type: ignore[assignment]

    (tmp_path / "ws-first").mkdir()
    (tmp_path / "ws-first" / "app").mkdir()
    (tmp_path / "ws-first" / "app" / "index.html").write_text(
        "<html><head><title>t</title>"
        '<meta name="description" content="d"></head><body>hi</body></html>',
        encoding="utf-8",
    )
    (tmp_path / "ws-first" / "sitemap.xml").write_text("<urlset></urlset>", encoding="utf-8")
    (tmp_path / "ws-first" / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")

    result = await dev_graph._verify_and_ship(
        ctx,
        session,
        product_id=product_id,
        product_name="Domain First",
        workspace="ws-first",
        test_command="pytest",
        idea_id=idea_id,
    )

    assert "approved and deployed" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.domain_status == "pending"
    assert product.vercel_project == "domain-first"

    interrupts = (
        (
            await session.execute(
                select(Interrupt).where(Interrupt.reason == InterruptReason.DOMAIN_REQUIRED.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(interrupts) == 1
    assert interrupts[0].product_id == product_id


async def test_a_redeploy_of_an_already_asked_product_never_asks_again(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from factory.engines.development import bug_council
    from factory.engines.development import deploy as deploy_module

    product_id = await _make_live_product(session, "domain-redeploy")
    await repository.set_domain_status(session, product_id, "pending")
    await session.flush()
    product = await session.get(Product, product_id)
    assert product is not None

    async def fake_convene(*args: object, **kwargs: object) -> CouncilResult:
        return CouncilResult(approved=True, verdicts=[])

    async def fake_deploy(*args: object, **kwargs: object) -> object:
        return deploy_module.DeployResult(
            ok=True,
            url="https://domain-redeploy.vercel.app",
            detail="ok",
            project="domain-redeploy",
        )

    monkeypatch.setattr(bug_council, "convene", fake_convene)
    monkeypatch.setattr(dev_graph.deploy_module, "deploy", fake_deploy)

    notifier = _RecordingNotifier()
    ctx = _ctx(workspace_root=tmp_path, notifier=notifier)
    ctx.sandbox = _PassingSandbox()  # type: ignore[assignment]

    (tmp_path / "ws-redeploy").mkdir()
    (tmp_path / "ws-redeploy" / "app").mkdir()
    (tmp_path / "ws-redeploy" / "app" / "index.html").write_text(
        "<html><head><title>t</title>"
        '<meta name="description" content="d"></head><body>hi</body></html>',
        encoding="utf-8",
    )
    (tmp_path / "ws-redeploy" / "sitemap.xml").write_text("<urlset></urlset>", encoding="utf-8")
    (tmp_path / "ws-redeploy" / "robots.txt").write_text("User-agent: *\n", encoding="utf-8")

    await dev_graph._verify_and_ship(
        ctx,
        session,
        product_id=product_id,
        product_name="Domain Redeploy",
        workspace="ws-redeploy",
        test_command="pytest",
        idea_id=product.idea_id,
    )

    count = (
        await session.execute(
            select(func.count())
            .select_from(Interrupt)
            .where(Interrupt.reason == InterruptReason.DOMAIN_REQUIRED.value)
        )
    ).scalar_one()
    assert count == 0


# --- ATTACH_DOMAIN task --------------------------------------------------------------


def _attach_task(product_id: str, hostname: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.ATTACH_DOMAIN.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=80,
        payload={"product_id": product_id, "hostname": hostname},
        product_id=product_id,
        attempt_count=1,
    )


async def test_attach_domain_task_sets_attached_when_not_yet_verified(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = await _make_live_product(session, "domain-attach-pending")
    await repository.set_product_domain(session, product_id, "example.com")
    await session.flush()

    async def fake_attach(
        settings: object, *, project: str, hostname: str
    ) -> domain_module.AttachResult:
        return domain_module.AttachResult(
            ok=True,
            verified=False,
            detail="attached",
            records=[domain_module.DnsRecord(type="TXT", name="_vercel", value="abc")],
        )

    monkeypatch.setattr(domain_module, "attach", fake_attach)

    notifier = _RecordingNotifier()
    ctx = _ctx(workspace_root=tmp_path, notifier=notifier)

    result = await dev_graph.run(ctx, session, _attach_task(product_id, "example.com"))

    assert "attached" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.domain_status == "attached"
    assert notifier.sent  # DNS instructions relayed


async def test_attach_domain_task_goes_straight_to_verified_when_vercel_says_so(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = await _make_live_product(session, "domain-attach-instant")
    await repository.set_product_domain(session, product_id, "example.com")
    await session.flush()

    async def fake_attach(
        settings: object, *, project: str, hostname: str
    ) -> domain_module.AttachResult:
        return domain_module.AttachResult(ok=True, verified=True, detail="attached")

    monkeypatch.setattr(domain_module, "attach", fake_attach)

    ctx = _ctx(workspace_root=tmp_path, notifier=_RecordingNotifier())
    await dev_graph.run(ctx, session, _attach_task(product_id, "example.com"))

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.domain_status == "verified"
    assert product.deploy_url == "https://example.com"


async def test_attach_domain_task_raises_devopserror_on_infra_failure(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = await _make_live_product(session, "domain-attach-infra")
    await repository.set_product_domain(session, product_id, "example.com")
    await session.flush()

    async def fake_attach(
        settings: object, *, project: str, hostname: str
    ) -> domain_module.AttachResult:
        return domain_module.AttachResult(
            ok=False, verified=False, detail="vercel 500", is_infra=True
        )

    monkeypatch.setattr(domain_module, "attach", fake_attach)

    ctx = _ctx(workspace_root=tmp_path, notifier=_RecordingNotifier())

    with pytest.raises(DevOpsError):
        await dev_graph.run(ctx, session, _attach_task(product_id, "example.com"))


# --- VERIFY_DOMAIN task ---------------------------------------------------------------


def _verify_task(product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.VERIFY_DOMAIN.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=115,
        payload={"product_id": product_id},
        product_id=product_id,
        attempt_count=1,
    )


async def test_verify_domain_confirms_when_vercel_reports_verified(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = await _make_live_product(session, "domain-verify-yes")
    await repository.set_product_domain(session, product_id, "example.com")
    await repository.set_domain_status(session, product_id, "attached")
    await session.flush()

    async def fake_check(settings: object, *, project: str, hostname: str) -> bool:
        return True

    monkeypatch.setattr(domain_module, "check_verified", fake_check)

    ctx = _ctx(workspace_root=tmp_path, notifier=_RecordingNotifier())
    result = await dev_graph.run(ctx, session, _verify_task(product_id))

    assert "verified" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.domain_status == "verified"
    assert product.deploy_url == "https://example.com"


async def test_verify_domain_stays_attached_when_not_yet_verified(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    product_id = await _make_live_product(session, "domain-verify-not-yet")
    await repository.set_product_domain(session, product_id, "example.com")
    await repository.set_domain_status(session, product_id, "attached")
    await session.flush()

    async def fake_check(settings: object, *, project: str, hostname: str) -> bool:
        return False

    monkeypatch.setattr(domain_module, "check_verified", fake_check)

    ctx = _ctx(workspace_root=tmp_path, notifier=_RecordingNotifier())
    await dev_graph.run(ctx, session, _verify_task(product_id))

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.domain_status == "attached"


# --- repository: products_with_attached_domain ---------------------------------------


async def test_products_with_attached_domain_only_returns_attached_status(
    session: AsyncSession,
) -> None:
    attached = await _make_live_product(session, "domain-query-attached")
    await repository.set_domain_status(session, attached, "attached")
    verified = await _make_live_product(session, "domain-query-verified")
    await repository.set_domain_status(session, verified, "verified")
    pending = await _make_live_product(session, "domain-query-pending")
    await repository.set_domain_status(session, pending, "pending")
    await session.flush()

    products = await repository.products_with_attached_domain(session)

    ids = {p.id for p in products}
    assert ids == {attached}
