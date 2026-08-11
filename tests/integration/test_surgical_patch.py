"""A surgical repair runs to completion while `_patch` holds the product lock.

This is the regression test for the failure that would have made the whole feature
unusable, and it is written against real Postgres because the bug is invisible
without it.

`_patch` holds `product_lock(holder=DEVELOPMENT)` for its entire body, and
`_run_engine` wraps the runner in one `session_scope` that commits only on clean
exit -- so that lock row stays *uncommitted* for the duration. The obvious way to
build the repair agent's tools would have been to reuse `run_in_sandbox`, which opens
its own session and acquires the same lock. That second `INSERT ... ON CONFLICT DO
NOTHING` cannot resolve against an uncommitted row of the same primary key, so
Postgres does not return "already locked" -- it *blocks*, waiting on the first
transaction. Every tool call would then hang until something cancelled it, and
because the factory's tick loop is sequential the whole factory would stall for
roughly half an hour per repair while making no progress at all.

The fix is that the workspace tools do not take the lock, because the only caller
that builds them already holds it. This test is what keeps that true: the assertion
that matters is not just "the repair worked" but that it worked *without hanging*,
which is why everything here runs under a hard timeout.

Needs Postgres:

    docker compose up -d postgres
    alembic upgrade head
    pytest tests/integration -q
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.engines.development import graph as dev_graph
from factory.engines.development import repair
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import AgentTurn, Provider, ToolCall, Usage
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.runner import SandboxResult
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.orm import Base, Product, Task
from factory.state.schemas import ProductStatus, ScoredIdea, TaskKind
from factory.supervisor.context import FactoryContext

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL is not set; integration tests need Postgres"
)

#: Generous for real work, unforgiving for a deadlock. Without this the regression
#: this file exists for presents as a hung test run, not a failure.
HARD_TIMEOUT_SECONDS = 60


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


class _FakeSandbox:
    """Reports a failing suite until the agent edits `main.py`, then a passing one."""

    available = True

    def __init__(self, workspace_root: Path, workspace: str) -> None:
        self._path = workspace_root / workspace / "app" / "main.py"
        self.commands: list[str] = []

    async def execute(
        self,
        command: str,
        *,
        workspace: str,
        timeout_seconds: int | None = None,
        network: bool = False,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        self.commands.append(command)
        if "pip install" in command:
            return SandboxResult(exit_code=0, stdout="installed", stderr="")
        fixed = "a + b" in self._path.read_text(encoding="utf-8")
        if fixed:
            return SandboxResult(exit_code=0, stdout="1 passed", stderr="")
        return SandboxResult(
            exit_code=1,
            stdout="1 failed",
            stderr="E  assert -1 == 3\nE   +  where -1 = add(1, 2)",
        )


def _agent_turn(text: str = "", calls: list[ToolCall] | None = None) -> AgentTurn:
    return AgentTurn(
        text=text,
        tool_calls=calls or [],
        stop_reason="end_turn" if not calls else "tool_use",
        provider=Provider.ANTHROPIC,
        model="claude-opus-5",
        usage=Usage(input_tokens=100, output_tokens=50),
    )


def _ctx(settings: Settings, sandbox: object, provider: FakeProvider) -> FactoryContext:
    ctx = FactoryContext(
        settings=settings,
        llm=LLMClient(ProviderRegistry({Provider.ANTHROPIC: provider}), settings=settings),
        notifier=NullNotifier(),
        sandbox=sandbox,  # type: ignore[arg-type]
        enabled_engines={EngineName.DEVELOPMENT},
        worker_id="surgical-test",
    )
    ctx.docker_available = True
    return ctx


def _settings(tmp_path: Path, blacklist: Path) -> Settings:
    return Settings(
        database_url=DATABASE_URL,
        anthropic_api_key="test-key",
        gemini_api_key="test-key",
        workspace_root=tmp_path,
        blacklist_path=blacklist,
        factory_dry_run=True,
    )


async def _seed_product(session: AsyncSession, settings: Settings) -> tuple[str, str]:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche="bookkeeping",
        source_url="https://example.test/thread",
        monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name="Reconciler", idea_id=idea_id)
    workspace = f"product-{product_id[:12]}"
    await repository.set_product_status(
        session,
        product_id,
        ProductStatus.PATCHING,
        workspace_path=workspace,
        test_command="pytest -q",
    )

    root = settings.workspace_root / workspace
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_main.py").write_text(
        "from app.main import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    return product_id, idea_id


async def _patch_task(session: AsyncSession, product_id: str, idea_id: str) -> Task:
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"patch:{product_id}:1",
        payload={
            "product_id": product_id,
            "idea_id": idea_id,
            "healing": "The test suite failed: assert -1 == 3",
            "category": "test_failure",
        },
        product_id=product_id,
    )
    assert task_id is not None
    task = await session.get(Task, task_id)
    assert task is not None
    return task


async def test_a_surgical_repair_completes_while_patch_holds_the_product_lock(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, blacklist_file)
    product_id, idea_id = await _seed_product(session, settings)
    workspace = f"product-{product_id[:12]}"
    sandbox = _FakeSandbox(tmp_path, workspace)

    provider = FakeProvider(Provider.ANTHROPIC)
    provider.register_turns(
        _agent_turn(
            calls=[
                ToolCall(id="1", name="run_workspace_command", arguments={"command": "pytest -q"})
            ]
        ),
        _agent_turn(
            calls=[ToolCall(id="2", name="read_workspace_file", arguments={"path": "app/main.py"})]
        ),
        _agent_turn(
            calls=[
                ToolCall(
                    id="3",
                    name="write_workspace_file",
                    arguments={
                        "path": "app/main.py",
                        "content": "def add(a, b):\n    return a + b\n",
                    },
                )
            ]
        ),
        _agent_turn(
            calls=[
                ToolCall(id="4", name="run_workspace_command", arguments={"command": "pytest -q"})
            ]
        ),
        _agent_turn(text="add() subtracted instead of adding. RESULT: FIXED, pytest -q exit 0"),
    )
    ctx = _ctx(settings, sandbox, provider)

    # Council and deploy are exercised in their own suites; stubbing them keeps this
    # test focused on the one thing it exists to prove -- that the agent's tool calls
    # do not block on the lock `_patch` is already holding.
    async def fake_convene(*args: object, **kwargs: object) -> object:
        from factory.state.schemas import CouncilResult

        return CouncilResult(approved=True, verdicts=[])

    async def fake_deploy(*args: object, **kwargs: object) -> object:
        from factory.engines.development.deploy import DeployResult

        return DeployResult(ok=True, url="https://example.test", detail="ok")

    monkeypatch.setattr(dev_graph.bug_council, "convene", fake_convene)
    monkeypatch.setattr(dev_graph.deploy_module, "deploy", fake_deploy)

    task = await _patch_task(session, product_id, idea_id)

    # The assertion that matters: this finishes at all. A regression here does not
    # fail, it hangs -- so it is bounded rather than awaited directly.
    result = await asyncio.wait_for(
        dev_graph._patch(ctx, session, task), timeout=HARD_TIMEOUT_SECONDS
    )

    assert "locked by another worker" not in result
    fixed = (tmp_path / workspace / "app" / "main.py").read_text(encoding="utf-8")
    assert "a + b" in fixed, "the agent's edit must survive to the end of the repair"
    # No regeneration happened: the whole point is that only the broken file moved.
    assert (tmp_path / workspace / "tests" / "test_main.py").read_text(encoding="utf-8")
    # The snapshot is cleaned up on success, never left behind to confuse a later run.
    assert not (tmp_path / f"{workspace}.snapshot").exists()


async def test_a_repair_that_deletes_the_failing_test_is_rolled_back(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    """Deleting the failing test makes the suite green, passes the Bug Council (there
    is nothing left to object to), and ships a product whose bug is now invisible.
    The workspace must come back exactly as it was, and the cycle must not proceed to
    verification."""
    settings = _settings(tmp_path, blacklist_file)
    product_id, idea_id = await _seed_product(session, settings)
    workspace = f"product-{product_id[:12]}"
    sandbox = _FakeSandbox(tmp_path, workspace)

    provider = FakeProvider(Provider.ANTHROPIC)
    provider.register_turns(
        _agent_turn(
            calls=[
                ToolCall(
                    id="1",
                    name="write_workspace_file",
                    arguments={"path": "tests/test_main.py", "content": "# removed\n"},
                )
            ]
        ),
        _agent_turn(text="Removed the failing test. RESULT: FIXED"),
    )
    ctx = _ctx(settings, sandbox, provider)
    task = await _patch_task(session, product_id, idea_id)

    result = await asyncio.wait_for(
        dev_graph._patch(ctx, session, task), timeout=HARD_TIMEOUT_SECONDS
    )

    restored = (tmp_path / workspace / "tests" / "test_main.py").read_text(encoding="utf-8")
    assert "assert add(1, 2) == 3" in restored, "the tampered test must be restored"
    assert "needs repair" in result or "escalated" in result
    product = await session.get(Product, product_id)
    assert product is not None
    assert product.heal_attempts == 1, "a rejected repair still consumes its cycle"


async def test_a_repair_that_changes_nothing_never_reaches_verification(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that explains the bug without touching a file reports "completed" just
    as loudly as one that fixed it. Running the gate on an unchanged workspace would
    spend a dependency install, a full suite and three Opus reviewers to rediscover
    the identical failure."""
    settings = _settings(tmp_path, blacklist_file)
    product_id, idea_id = await _seed_product(session, settings)
    sandbox = _FakeSandbox(tmp_path, f"product-{product_id[:12]}")

    provider = FakeProvider(Provider.ANTHROPIC)
    provider.register_turns(
        _agent_turn(text="The bug is that add() subtracts. Someone should fix it.")
    )
    ctx = _ctx(settings, sandbox, provider)

    convened = False

    async def fake_convene(*args: object, **kwargs: object) -> object:
        nonlocal convened
        convened = True
        raise AssertionError("the Bug Council must not run for an unchanged workspace")

    monkeypatch.setattr(dev_graph.bug_council, "convene", fake_convene)
    task = await _patch_task(session, product_id, idea_id)

    await asyncio.wait_for(dev_graph._patch(ctx, session, task), timeout=HARD_TIMEOUT_SECONDS)

    assert convened is False
    assert not any("pytest" in c for c in sandbox.commands), (
        "the verification suite must not run either"
    )


async def test_a_provider_outage_defers_instead_of_consuming_a_heal_cycle(
    session: AsyncSession, tmp_path: Path, blacklist_file: Path
) -> None:
    """`AgentRuntime` converts an outage into a normal result with `stopped_because`
    set, so without special handling it would look like a failed repair. Three
    outages in a row would then permanently escalate a product whose repair was never
    actually attempted."""
    settings = _settings(tmp_path, blacklist_file)
    product_id, idea_id = await _seed_product(session, settings)
    workspace = f"product-{product_id[:12]}"
    sandbox = _FakeSandbox(tmp_path, workspace)

    from factory.llm.providers.base import ProviderUnavailable

    provider = FakeProvider(Provider.ANTHROPIC)
    provider.register_turns(ProviderUnavailable(Provider.ANTHROPIC, "503", status=503))
    ctx = _ctx(settings, sandbox, provider)
    task = await _patch_task(session, product_id, idea_id)

    with pytest.raises(repair.RepairBlocked):
        await asyncio.wait_for(dev_graph._patch(ctx, session, task), timeout=HARD_TIMEOUT_SECONDS)

    product = await session.get(Product, product_id)
    assert product is not None
    assert product.heal_attempts == 0, "an outage must not count against the heal budget"
    assert not (tmp_path / f"{workspace}.snapshot").exists(), "the rollback must have run"
