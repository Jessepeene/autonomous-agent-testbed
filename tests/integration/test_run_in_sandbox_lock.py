"""`run_in_sandbox`'s previously-missing contention protection.

Regression coverage for a bug found in the 2026-08-10 full-codebase audit: the
tool handler an ephemeral agent uses to run shell commands against a product's
workspace never acquired `product_lock`, unlike development/graph.py's own
_build()/_patch() writers -- an agent spawned by a maintenance task (the only real
caller with sandbox access, RoleName.REPAIR_ENGINEER) could run commands against a
workspace at the exact moment Development was mid clear_workspace()/write_plan()
for that same product, with no contention protection at all.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from factory.sandbox.runner import SandboxResult
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Base
from factory.state.schemas import ProductStatus, ScoredIdea
from factory.supervisor.concurrency import product_lock

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
    """run_in_sandbox opens its own session via session_scope() -- the process-wide
    cached engine, a different connection than this file's own `session` fixture."""
    yield
    from factory.state import db as db_module

    await db_module.dispose_engine()


@dataclass(slots=True)
class _FakeSandbox:
    calls: list[str]

    async def execute(self, command: str, *, workspace: str, **kwargs: object) -> SandboxResult:
        self.calls.append(workspace)
        return SandboxResult(exit_code=0, stdout="ok", stderr="")


async def _make_product_with_workspace(session: AsyncSession, workspace: str) -> str:
    idea = ScoredIdea(
        problem_statement="Bookkeepers reconcile invoices by hand every week.",
        target_niche="run_in_sandbox lock test",
        source_url="https://example.test/thread",
        monetization_hypothesis="EUR 49/month per seat.",
        pain_severity=0.8,
        willingness_to_pay=0.7,
        build_feasibility=0.6,
        adoption_ease=0.8,
    )
    idea_id = await repository.upsert_idea(session, idea)
    assert idea_id is not None
    product_id = await repository.create_product(session, name="Lock Test", idea_id=idea_id)
    await repository.set_product_status(
        session, product_id, ProductStatus.PLANNED, workspace_path=workspace
    )
    return product_id


async def test_run_in_sandbox_refuses_when_development_holds_the_lock(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from factory.settings import get_settings

    get_settings.cache_clear()

    product_id = await _make_product_with_workspace(session, "product-locked")
    await session.commit()

    async with product_lock(
        session, product_id=product_id, holder=EngineName.DEVELOPMENT, reason="patching"
    ):
        # Committed while still "inside" the lock's lifetime (release happens in
        # product_lock's own finally, on context exit) -- run_in_sandbox below
        # acquires its lock through a *different* connection (session_scope()'s
        # process-wide cached engine), and an uncommitted row from this session is
        # not visible to a concurrent INSERT ... ON CONFLICT on another connection.
        await session.commit()

        import factory.sandbox.runner as runner_module
        from factory.tools.builtin import run_in_sandbox

        fake = _FakeSandbox(calls=[])
        monkeypatch.setattr(runner_module, "get_sandbox", lambda settings=None: fake)

        result = await run_in_sandbox("echo hi", "product-locked")

        assert result["exit_code"] == -1
        assert "locked" in result["stderr"]
        assert fake.calls == []  # the sandbox itself was never actually invoked

    get_settings.cache_clear()


async def test_run_in_sandbox_proceeds_when_no_lock_is_held(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from factory.settings import get_settings

    get_settings.cache_clear()

    await _make_product_with_workspace(session, "product-free")
    await session.commit()

    import factory.sandbox.runner as runner_module
    from factory.tools.builtin import run_in_sandbox

    fake = _FakeSandbox(calls=[])
    monkeypatch.setattr(runner_module, "get_sandbox", lambda settings=None: fake)

    result = await run_in_sandbox("echo hi", "product-free")

    assert result["exit_code"] == 0
    assert fake.calls == ["product-free"]

    get_settings.cache_clear()


async def test_run_in_sandbox_proceeds_for_a_workspace_with_no_tracked_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every workspace belongs to a tracked product (ad hoc/test workspaces) --
    nothing to protect against contention over, so the command still runs."""
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL or "")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from factory.settings import get_settings

    get_settings.cache_clear()

    import factory.sandbox.runner as runner_module
    from factory.tools.builtin import run_in_sandbox

    fake = _FakeSandbox(calls=[])
    monkeypatch.setattr(runner_module, "get_sandbox", lambda settings=None: fake)

    result = await run_in_sandbox("echo hi", "no-such-workspace")

    assert result["exit_code"] == 0
    assert fake.calls == ["no-such-workspace"]

    get_settings.cache_clear()
