"""AUDITOR's live-product walkthrough -- the Development department's standing
oversight, and the first agent in this factory that *uses* a product rather than
reading its markup.

The incident it exists for: a live product shipped with a signup form that
silently failed on submit and a hardcoded year that had gone stale, and both
existing checks (`check_deploy_url`'s status code, `_content_freshness_issue`'s
markup review) called it healthy for three weeks.

The design these tests pin down came out of that feature's own first two live
runs, which both failed: the agent had a `report_product_issue` tool and spent
its entire step budget investigating without ever calling it, so a real review
was performed and then thrown away. The verdict is now *extracted* from whatever
the agent said, always -- the agent no longer decides whether its findings get
acted on, only what they are.

No real browser is launched here -- `BrowserSession` is replaced with a stub that
yields the same tool shape. What is exercised for real: the dispatch branch, the
origin pinning, verdict extraction (including from a truncated run), and the
patch it commissions.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.development import graph as dev_graph
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import AgentTurn, Provider, ToolCall, Usage
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings, get_settings
from factory.state import repository
from factory.state.orm import Base, Task, new_id
from factory.state.schemas import ProductStatus, ScoredIdea, TaskKind, TaskStatus
from factory.supervisor.context import FactoryContext
from factory.tools.product_review import ReviewVerdict
from factory.tools.registry import ToolDescriptor, ToolRegistry

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)

DEPLOY_URL = "https://example-product.test"


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
    get_settings.cache_clear()


def _ctx(gemini: FakeProvider, *, workspace_root: Path, dry_run: bool = False) -> FactoryContext:
    settings = Settings(
        database_url=DATABASE_URL,
        gemini_api_key="test-key",
        workspace_root=workspace_root,
        factory_dry_run=dry_run,
    )
    llm = LLMClient(ProviderRegistry({Provider.GEMINI: gemini}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT},
        worker_id="live-review-test",
    )


async def _make_product(
    session: AsyncSession, niche: str, *, deploy_url: str | None = DEPLOY_URL
) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every single week.",
        target_niche=niche,
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month, bought after the first month-end close.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name=niche, idea_id=idea_id)
    if deploy_url:
        await repository.set_product_status(
            session, product_id, ProductStatus.LIVE, deploy_url=deploy_url
        )
    return product_id


def _review_task(product_id: str) -> Task:
    return Task(
        id=new_id(),
        kind=TaskKind.REVIEW_LIVE_PRODUCT.value,
        engine=EngineName.DEVELOPMENT.value,
        status=TaskStatus.CLAIMED.value,
        priority=64,
        payload={"product_id": product_id},
        product_id=product_id,
        attempt_count=1,
    )


def _turn(text: str = "", calls: list[ToolCall] | None = None) -> AgentTurn:
    return AgentTurn(
        text=text,
        tool_calls=calls or [],
        stop_reason="tool_use" if calls else "end_turn",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=5, output_tokens=5),
    )


def _verdict(
    defect: bool, summary: str = "", details: str = "", *, surface: str = "product"
) -> ReviewVerdict:
    return ReviewVerdict(defect_found=defect, summary=summary, details=details, surface=surface)


class _StubBrowserSession:
    """Same shape as the real `BrowserSession`, no Chromium. Records the origin
    allowlist it was constructed with so the pinning can be asserted."""

    last_allowed: ClassVar[set[str]] = set()

    def __init__(self, *, allowed_origins: set[str]) -> None:
        _StubBrowserSession.last_allowed = set(allowed_origins)

    async def __aenter__(self) -> ToolRegistry:
        registry = ToolRegistry()

        async def browser_open(url: str) -> dict[str, Any]:
            return {
                "url": url,
                "title": "Reconcile faster",
                "text": "Join the waitlist. Copyright 2019.",
                "form_fields": [{"tag": "input", "type": "email", "name": "email"}],
                "network": [{"method": "GET", "url": url, "status": 200}],
                "console_errors": [],
            }

        registry.register(
            ToolDescriptor(
                name="browser_open",
                description="stub",
                input_schema={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                    "additionalProperties": False,
                },
                category="browser",
                handler=browser_open,
            )
        )
        return registry

    async def __aexit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def stub_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    # `_review_live_product` imports BrowserSession lazily from this module, so
    # patching the source module is what the deferred import actually resolves.
    monkeypatch.setattr("factory.tools.browser.BrowserSession", _StubBrowserSession)


@pytest.fixture
def live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`queue_review_patch` opens its own session via `get_settings()`."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()


async def _patches(session: AsyncSession) -> list[Task]:
    rows = await session.execute(select(Task).where(Task.kind == TaskKind.PATCH_PRODUCT.value))
    return list(rows.scalars().all())


async def _journey_patches(session: AsyncSession) -> list[Task]:
    rows = await session.execute(
        select(Task).where(Task.kind == TaskKind.PATCH_SIGNUP_JOURNEY.value)
    )
    return list(rows.scalars().all())


# --- dispatch guards ---------------------------------------------------------


async def test_review_is_skipped_for_a_product_with_no_deploy_url(
    session: AsyncSession, tmp_path: Path
) -> None:
    product_id = await _make_product(session, "review-no-url", deploy_url=None)
    await session.flush()

    result = await dev_graph.run(
        _ctx(FakeProvider(), workspace_root=tmp_path), session, _review_task(product_id)
    )

    assert "no deploy_url" in result


async def test_review_is_skipped_for_an_unknown_product(
    session: AsyncSession, tmp_path: Path
) -> None:
    result = await dev_graph.run(
        _ctx(FakeProvider(), workspace_root=tmp_path), session, _review_task("not-a-real-id")
    )
    assert "not found" in result


# --- the reviewer finds something and a fix gets commissioned ----------------


async def test_a_finding_queues_exactly_one_guided_patch(
    session: AsyncSession, tmp_path: Path, stub_browser: None, live_env: None
) -> None:
    """The whole point: the reviewer used the page, said what was wrong in prose,
    and the extraction turned that into real work through the existing
    PATCH_PRODUCT lever -- not a log line nobody reads."""
    product_id = await _make_product(session, "review-finds-defect")
    await session.commit()

    gemini = FakeProvider()
    gemini.register_turns(
        _turn(calls=[ToolCall(id="1", name="browser_open", arguments={"url": DEPLOY_URL})]),
        _turn(
            text=(
                "Filled the email field and clicked Join. network shows "
                "POST /api/subscribe -> 404 and no confirmation rendered. The footer "
                "also still reads Copyright 2019."
            )
        ),
    )
    gemini.register(
        ReviewVerdict,
        _verdict(
            True,
            "The waitlist form does nothing on submit",
            "Submitting the email fired POST /api/subscribe which returned 404; no "
            "confirmation shown. Footer year is also stale (2019).",
        ),
    )

    result = await dev_graph.run(
        _ctx(gemini, workspace_root=tmp_path), session, _review_task(product_id)
    )
    assert "patch queued" in result

    patches = await _patches(session)
    assert len(patches) == 1
    assert patches[0].product_id == product_id
    assert patches[0].payload["category"] == "guided"
    assert "404" in patches[0].payload["healing"]


async def test_a_signup_journey_finding_is_never_sent_to_a_code_repair(
    session: AsyncSession, tmp_path: Path, stub_browser: None, live_env: None
) -> None:
    """The fork `ReviewVerdict.surface` exists for. Before it, a finding about the
    confirmation email or the page its link leads to had exactly one lever --
    PATCH_PRODUCT, which spawns a repair engineer against the product's own
    workspace -- and neither surface is part of that workspace at all. A repair
    engineer given that brief would have no file to open."""
    product_id = await _make_product(session, "review-finds-journey-defect")
    await session.commit()

    gemini = FakeProvider()
    gemini.register_turns(
        _turn(calls=[ToolCall(id="1", name="browser_open", arguments={"url": DEPLOY_URL})]),
        _turn(
            text=(
                "Submitted the form, read the confirmation mail through "
                "read_sent_messages, and opened the confirmation link. The page it "
                "leads to is plain monospace text served from the factory's own "
                "domain, with no mention of the product at all."
            )
        ),
    )
    gemini.register(
        ReviewVerdict,
        _verdict(
            True,
            "The confirmation page reads like a system message, not the product",
            "Opened the link from the confirmation mail. It is bare monospace "
            "text with no product name and no branding.",
            surface="signup_journey",
        ),
    )

    result = await dev_graph.run(
        _ctx(gemini, workspace_root=tmp_path), session, _review_task(product_id)
    )
    assert "signup-journey" in result

    assert await _patches(session) == [], "must never reach the product's own workspace"
    journey_patches = await _journey_patches(session)
    assert len(journey_patches) == 1
    assert journey_patches[0].product_id == product_id
    assert "confirmation page" in journey_patches[0].payload["finding_summary"]


async def test_a_review_that_ran_out_of_steps_still_yields_its_verdict(
    session: AsyncSession, tmp_path: Path, stub_browser: None, live_env: None
) -> None:
    """The regression that motivated the redesign. Both of this feature's first
    live runs exhausted their step budget mid-investigation and never called the
    reporting tool they had, so a real review was performed and discarded.
    Extraction runs on whatever the agent did say, so a truncated run still
    produces a decision."""
    product_id = await _make_product(session, "review-truncated")
    await session.commit()

    gemini = FakeProvider()
    # Text on an early turn, then nothing but tool calls until the ceiling --
    # exactly the shape that previously stranded the finding.
    gemini.register_turns(
        _turn(text="Submit fired POST /api/subscribe -> 500. That looks broken."),
        *[
            _turn(calls=[ToolCall(id=str(i), name="browser_open", arguments={"url": DEPLOY_URL})])
            for i in range(2, 20)
        ],
    )
    gemini.register(
        ReviewVerdict,
        _verdict(
            True,
            "Waitlist submit returns a server error",
            "Submitting the form fired POST /api/subscribe which returned HTTP 500.",
        ),
    )

    await dev_graph.run(_ctx(gemini, workspace_root=tmp_path), session, _review_task(product_id))

    assert len(await _patches(session)) == 1


async def test_the_browser_is_pinned_to_the_products_own_origin(
    session: AsyncSession, tmp_path: Path, stub_browser: None, live_env: None
) -> None:
    product_id = await _make_product(session, "review-origin-pin")
    await session.commit()

    gemini = FakeProvider()
    gemini.register_turns(_turn(text="Looks fine."))
    gemini.register(ReviewVerdict, _verdict(False))
    await dev_graph.run(_ctx(gemini, workspace_root=tmp_path), session, _review_task(product_id))

    assert _StubBrowserSession.last_allowed == {"https://example-product.test"}


async def test_a_clean_review_queues_no_work_at_all(
    session: AsyncSession, tmp_path: Path, stub_browser: None, live_env: None
) -> None:
    """'Most products most weeks need nothing' has to actually hold, or this
    becomes a machine for manufacturing busywork."""
    product_id = await _make_product(session, "review-all-clear")
    await session.commit()

    gemini = FakeProvider()
    gemini.register_turns(
        _turn(calls=[ToolCall(id="1", name="browser_open", arguments={"url": DEPLOY_URL})]),
        _turn(text="Signed up fine -- POST /api/subscribe returned 200. Nothing to flag."),
    )
    gemini.register(ReviewVerdict, _verdict(False))

    await dev_graph.run(_ctx(gemini, workspace_root=tmp_path), session, _review_task(product_id))

    assert await _patches(session) == []


async def test_a_defect_claimed_with_no_specifics_is_not_actionable(
    session: AsyncSession, tmp_path: Path, stub_browser: None, live_env: None
) -> None:
    """A verdict asserting something is wrong but naming nothing concrete would
    burn a real repair cycle chasing prose."""
    product_id = await _make_product(session, "review-vague")
    await session.commit()

    gemini = FakeProvider()
    gemini.register_turns(_turn(text="Something feels off about this page."))
    gemini.register(ReviewVerdict, _verdict(True, "", ""))

    result = await dev_graph.run(
        _ctx(gemini, workspace_root=tmp_path), session, _review_task(product_id)
    )
    assert "no specifics" in result
    assert await _patches(session) == []


async def test_dry_run_records_the_finding_but_commissions_nothing(
    session: AsyncSession, tmp_path: Path, stub_browser: None, live_env: None
) -> None:
    """AUDITOR is constructed directly (it needs an injected private registry), so
    `spawner.can_spawn`'s dry-run refusal never runs for it -- `queue_review_patch`
    enforces the same rule itself."""
    product_id = await _make_product(session, "review-dry-run")
    await session.commit()

    gemini = FakeProvider()
    gemini.register_turns(_turn(text="Submit fired POST /api/subscribe -> 404."))
    gemini.register(
        ReviewVerdict,
        _verdict(True, "Waitlist form is broken", "Submitting fired POST /api/subscribe -> 404."),
    )

    result = await dev_graph.run(
        _ctx(gemini, workspace_root=tmp_path, dry_run=True), session, _review_task(product_id)
    )
    assert "DRY_RUN" in result
    assert await _patches(session) == []


async def test_a_second_finding_the_same_day_does_not_stack_a_second_patch(
    session: AsyncSession, tmp_path: Path, stub_browser: None, live_env: None
) -> None:
    """Two competing rewrites of the same workspace is worse than one: the second
    would be generated against a tree the first is already changing."""
    product_id = await _make_product(session, "review-no-stacking")
    await session.commit()

    for _ in range(2):
        gemini = FakeProvider()
        gemini.register_turns(_turn(text="Submit fired POST /api/subscribe -> 404."))
        gemini.register(
            ReviewVerdict,
            _verdict(
                True, "Waitlist form is broken", "Submitting fired POST /api/subscribe -> 404."
            ),
        )
        await dev_graph.run(
            _ctx(gemini, workspace_root=tmp_path), session, _review_task(product_id)
        )

    assert len(await _patches(session)) == 1
