"""Repair passes see the existing workspace, and stale files never survive.

Before this, every heal cycle called `generate_scaffold` with zero visibility into
the files a previous attempt already wrote, and `write_plan` only ever added files,
never removed them. Two generations for the same product routinely disagree on file
layout, so both versions -- and both sets of bugs -- piled up side by side.
Observed live: Bug Council rejected a build for exactly this, having found the same
N+1 query bug duplicated across `app/main.py` and `backend/main.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory.engines.development.scaffold.generator import (
    clear_workspace,
    generate_landing_page,
    generate_scaffold,
    write_plan,
)
from factory.hitl.telegram import NullNotifier
from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.sandbox.disabled import DisabledSandbox
from factory.settings import EngineName, Settings
from factory.state.schemas import GeneratedFile, ScaffoldPlan, ValidatedIdea
from factory.supervisor.context import FactoryContext

_IDEA = ValidatedIdea(
    id="idea-1",
    problem_statement="Bookkeepers reconcile invoices by hand every week.",
    target_niche="bookkeeping",
    source_url="https://example.test/thread",
    monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
    score=0.8,
    evidence_count=3,
    created_at=datetime.now(UTC),
)

_PLAN = ScaffoldPlan(
    product_name="Invoice Bot",
    summary="A tool that reconciles invoices automatically for small bookkeepers.",
    files=[GeneratedFile(path="app/main.py", contents="print('hi')")],
    test_command="pytest",
)


def _ctx(*, provider: FakeProvider, workspace_root: Path) -> FactoryContext:
    settings = Settings(gemini_api_key="test-key", workspace_root=workspace_root)
    llm = LLMClient(ProviderRegistry({Provider.ANTHROPIC: provider}), settings=settings)
    return FactoryContext(
        settings=settings,
        llm=llm,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DEVELOPMENT},
    )


async def test_a_fresh_build_prompt_has_no_existing_contents_section(tmp_path: Path) -> None:
    provider = FakeProvider(name=Provider.ANTHROPIC)
    provider.register(ScaffoldPlan, _PLAN)
    ctx = _ctx(provider=provider, workspace_root=tmp_path)

    await generate_scaffold(ctx, _IDEA, product_id="prod-1")

    assert "CURRENT CONTENTS" not in provider.calls[-1]["prompt"]


async def test_a_repair_pass_prompt_includes_the_existing_files(tmp_path: Path) -> None:
    provider = FakeProvider(name=Provider.ANTHROPIC)
    provider.register(ScaffoldPlan, _PLAN)
    ctx = _ctx(provider=provider, workspace_root=tmp_path)
    existing = {"app/main.py": "print('the old broken version')"}

    await generate_scaffold(ctx, _IDEA, product_id="prod-1", existing_files=existing)

    prompt = provider.calls[-1]["prompt"]
    assert "CURRENT CONTENTS" in prompt
    assert "app/main.py" in prompt
    assert "print('the old broken version')" in prompt


# --- generate_landing_page: the AB-test variant-split prompt --------------------

_LANDING_PLAN = ScaffoldPlan(
    product_name="Invoice Bot",
    summary="A tool that reconciles invoices automatically for small bookkeepers.",
    files=[GeneratedFile(path="app/index.html", contents="<html></html>")],
    test_command="pytest",
)


async def test_without_ab_variants_the_prompt_carries_no_variant_instructions(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(name=Provider.ANTHROPIC)
    provider.register(ScaffoldPlan, _LANDING_PLAN)
    ctx = _ctx(provider=provider, workspace_root=tmp_path)

    await generate_landing_page(ctx, _IDEA, product_id="prod-1")

    prompt = provider.calls[-1]["prompt"]
    assert "AB test" not in prompt
    assert "Variant A brief" not in prompt


async def test_ab_variants_add_the_dual_variant_instructions_with_both_briefs(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(name=Provider.ANTHROPIC)
    provider.register(ScaffoldPlan, _LANDING_PLAN)
    ctx = _ctx(provider=provider, workspace_root=tmp_path)

    await generate_landing_page(
        ctx,
        _IDEA,
        product_id="prod-1",
        ab_variants=("Emphasize speed", "Emphasize price"),
    )

    prompt = provider.calls[-1]["prompt"]
    assert "live AB test" in prompt
    assert "Emphasize speed" in prompt
    assert "Emphasize price" in prompt
    assert 'id="variant-a"' in prompt
    assert 'id="variant-b"' in prompt
    # Both the interest form and the analytics beacon must be told to tag `variant`.
    assert prompt.count("variant") >= 4


def test_clear_workspace_removes_files_and_subdirectories(tmp_path: Path) -> None:
    settings = Settings(gemini_api_key="test-key", workspace_root=tmp_path)
    write_plan(_PLAN, "product-1", settings)
    root = tmp_path / "product-1"
    (root / "backend").mkdir()
    (root / "backend" / "main.py").write_text("duplicate copy")
    assert (root / "app" / "main.py").exists()
    assert (root / "backend" / "main.py").exists()

    clear_workspace("product-1", settings)

    assert root.exists()
    assert list(root.iterdir()) == []


def test_clear_workspace_on_an_already_empty_workspace_does_not_crash(tmp_path: Path) -> None:
    settings = Settings(gemini_api_key="test-key", workspace_root=tmp_path)

    clear_workspace("product-1", settings)

    assert (tmp_path / "product-1").exists()


def test_clear_workspace_logs_a_deletion_failure_instead_of_swallowing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: `ignore_errors=True` previously swallowed every deletion
    failure (a permission error, a locked file, a symlink edge case) with no trace
    at all -- exactly the "stale files silently persist" failure this function
    exists to prevent, one layer deeper. Must not crash the caller either way."""
    import os

    from factory.engines.development.scaffold import generator as generator_module

    settings = Settings(gemini_api_key="test-key", workspace_root=tmp_path)
    write_plan(_PLAN, "product-1", settings)

    logged: list[dict[str, object]] = []
    monkeypatch.setattr(
        generator_module.log,
        "warning",
        lambda event, **kwargs: logged.append({"event": event, **kwargs}),
    )

    real_unlink = os.unlink

    def failing_unlink(path: object, *args: object, **kwargs: object) -> None:
        if "main.py" in str(path):
            raise PermissionError("simulated: file is locked")
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", failing_unlink)

    clear_workspace("product-1", settings)  # must not raise

    assert any(entry["event"] == "scaffold.clear_workspace_failed" for entry in logged)


def test_a_second_write_plan_after_clear_leaves_no_trace_of_the_old_layout(
    tmp_path: Path,
) -> None:
    """The end-to-end guarantee: a patch cycle that renames app/ to backend/ must not
    leave both trees on disk."""
    settings = Settings(gemini_api_key="test-key", workspace_root=tmp_path)
    write_plan(_PLAN, "product-1", settings)

    renamed_plan = ScaffoldPlan(
        product_name="Invoice Bot",
        summary="A tool that reconciles invoices automatically for small bookkeepers.",
        files=[GeneratedFile(path="backend/main.py", contents="print('fixed')")],
        test_command="pytest",
    )
    clear_workspace("product-1", settings)
    write_plan(renamed_plan, "product-1", settings)

    root = tmp_path / "product-1"
    assert not (root / "app").exists()
    assert (root / "backend" / "main.py").read_text() == "print('fixed')"
