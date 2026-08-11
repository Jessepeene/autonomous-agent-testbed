"""The staging harness: mock the expensive calls, leave the cheap ones real.

`FACTORY_DRY_RUN` stubs outward side effects but never an LLM call, in either mode --
`CODE_GENERATION`/`CODE_REVIEW` (MVP scaffolds, landing pages, Bug Council) are 100%
Anthropic-served with no failover and the large majority of real spend. This mode
swaps only that provider for a deterministic fixture, so the rest of the pipeline can
be exercised repeatedly without paying Opus prices on every run.

The bar for the fixture content is not "looks plausible" -- it has to actually pass
the real deterministic Bug Council scanners (secrets/privilege) and, for the MVP
fixture, actually run under a real pytest invocation, since those are the two checks
this file can exercise without a Docker sandbox.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from factory.engines.development.bug_council import scan_privilege, scan_secrets
from factory.llm.client import build_registry
from factory.llm.providers.base import Provider
from factory.llm.providers.staging import StagingCodeProvider
from factory.settings import Settings, TaskType
from factory.state.schemas import ComplianceAssessment, ReviewVerdict, ScaffoldPlan


def _sources(plan: ScaffoldPlan) -> dict[str, str]:
    return {f.path: f.contents for f in plan.files}


# --- ScaffoldPlan: landing page vs. MVP -------------------------------------------


async def test_a_landing_page_prompt_yields_the_landing_page_fixture() -> None:
    provider = StagingCodeProvider()
    prompt = (
        "Validated idea:\n- problem: p\n\n"
        'Interest capture URL (POST JSON {"email": ..., "message": ...} here, '
        "exactly, verbatim): https://factory.test/interest/prod-123\n\n"
    )

    completion = await provider.parse(
        ScaffoldPlan,
        system="you are the validation agent",
        prompt=prompt,
        task_type=TaskType.CODE_GENERATION,
        model="claude-opus-5",
    )

    sources = _sources(completion.value)
    assert "app/index.html" in sources
    assert "https://factory.test/interest/prod-123" in sources["app/index.html"]
    # The MVP-only surface must not leak into a landing page.
    assert "stripe" not in sources["app/server.py"].lower()
    assert "app/billing" not in sources


async def test_an_mvp_prompt_yields_the_auth_and_billing_fixture() -> None:
    provider = StagingCodeProvider()

    completion = await provider.parse(
        ScaffoldPlan,
        system="you are the engineering agent",
        prompt="Validated idea:\n- problem: p\n\nProduce the complete MVP.",
        task_type=TaskType.CODE_GENERATION,
        model="claude-opus-5",
    )

    sources = _sources(completion.value)
    assert "stripe" in sources["app/server.py"].lower()
    assert "client_reference_id" in sources["app/server.py"]
    assert "PRODUCT_ID" in sources["app/server.py"]


async def test_the_interest_url_falls_back_safely_when_the_prompt_has_no_match() -> None:
    """A malformed or missing marker must not crash generation -- it should degrade
    to a placeholder URL, not raise, since this stands in for a real provider call
    that always returns *something* schema-valid."""
    provider = StagingCodeProvider()

    completion = await provider.parse(
        ScaffoldPlan,
        system="you are the validation agent",
        prompt="Interest capture URL (exactly, verbatim): but no colon-space match here",
        task_type=TaskType.CODE_GENERATION,
        model="claude-opus-5",
    )

    assert "app/index.html" in _sources(completion.value)


async def test_usage_is_zero_since_no_real_call_happened() -> None:
    provider = StagingCodeProvider()
    completion = await provider.parse(
        ScaffoldPlan,
        system="you are the engineering agent",
        prompt="Produce the complete MVP.",
        task_type=TaskType.CODE_GENERATION,
        model="claude-opus-5",
    )
    assert completion.usage.input_tokens == 0
    assert completion.usage.output_tokens == 0


# --- ReviewVerdict: category fidelity ----------------------------------------------


async def test_each_reviewer_category_gets_back_its_own_category_approved() -> None:
    from factory.engines.development.bug_council import REVIEWER_PROMPTS

    provider = StagingCodeProvider()
    for category, system_prompt in REVIEWER_PROMPTS.items():
        completion = await provider.parse(
            ReviewVerdict,
            system=system_prompt,
            prompt="Emit your verdict for the codebase above.",
            task_type=TaskType.CODE_REVIEW,
            model="claude-opus-5",
        )
        assert completion.value.category == category
        assert completion.value.approved is True
        assert completion.value.findings == []


# --- unanticipated schema: fail loudly, not silently -------------------------------


async def test_an_unrecognised_schema_raises_rather_than_guessing() -> None:
    provider = StagingCodeProvider()
    with pytest.raises(AssertionError, match="ComplianceAssessment"):
        await provider.parse(
            ComplianceAssessment,
            system="s",
            prompt="p",
            task_type=TaskType.COMPLIANCE,
            model="claude-opus-5",
        )


# --- converse: a safe default, never a crash ---------------------------------------


async def test_converse_returns_a_completed_turn_with_no_tool_calls() -> None:
    """Reached only if a heal cycle somehow fires against a staging-generated
    product. Must not fabricate a plausible-looking tool call against a real
    workspace -- report nothing to fix instead."""
    provider = StagingCodeProvider()
    turn = await provider.converse(
        system="s", turns=[], tools=[], task_type=TaskType.CODE_GENERATION, model="claude-opus-5"
    )
    assert turn.tool_calls == []
    assert turn.stop_reason == "end_turn"


# --- the fixtures actually pass the real, deterministic Bug Council scanners ------


async def test_the_landing_page_fixture_passes_the_real_secrets_and_privilege_scans() -> None:
    provider = StagingCodeProvider()
    completion = await provider.parse(
        ScaffoldPlan,
        system="validation agent",
        prompt="Interest capture URL (exactly, verbatim): https://factory.test/interest/p1",
        task_type=TaskType.CODE_GENERATION,
        model="claude-opus-5",
    )
    sources = _sources(completion.value)
    assert scan_secrets(sources).approved is True
    assert scan_privilege(sources).approved is True


async def test_the_mvp_fixture_passes_the_real_secrets_and_privilege_scans() -> None:
    provider = StagingCodeProvider()
    completion = await provider.parse(
        ScaffoldPlan,
        system="engineering agent",
        prompt="Produce the complete MVP.",
        task_type=TaskType.CODE_GENERATION,
        model="claude-opus-5",
    )
    sources = _sources(completion.value)
    assert scan_secrets(sources).approved is True
    assert scan_privilege(sources).approved is True


# --- the MVP fixture's own test suite genuinely passes under a real pytest run ----


def test_the_mvp_fixture_suite_actually_passes(tmp_path: Path) -> None:
    """The point of this whole mode is that the *rest* of the pipeline runs for
    real -- including a real pytest run inside the sandbox. If this fixture's own
    tests don't genuinely pass, staging mode would make every build heal-cycle and
    eventually escalate, which defeats the entire point.

    Deliberately does NOT `pip install` anything: fastapi/httpx/stripe are already
    real dependencies of this project (`pyproject.toml`), installed in whatever
    environment is running this suite. A previous version of this test ran
    `pip install -r <fixture's requirements.txt>` with `sys.executable` -- `cwd=`
    only changes the working directory, not the target environment, so that
    silently installed/downgraded packages into the *real* shared dev venv (caught
    when it downgraded `stripe` from 15.x to 11.x and broke an unrelated test).
    Running pytest directly against the fixture, relying on what's already
    installed, gets the same verification with no side effect on the environment
    running this test.
    """
    import asyncio

    async def _get_plan() -> ScaffoldPlan:
        provider = StagingCodeProvider()
        completion = await provider.parse(
            ScaffoldPlan,
            system="engineering agent",
            prompt="Produce the complete MVP.",
            task_type=TaskType.CODE_GENERATION,
            model="claude-opus-5",
        )
        return completion.value

    plan = asyncio.run(_get_plan())

    for f in plan.files:
        target = tmp_path / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.contents, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- build_registry wiring ----------------------------------------------------------


def test_staging_flag_swaps_only_the_anthropic_provider(tmp_path: Path) -> None:
    from factory.llm.providers.anthropic_provider import AnthropicProvider
    from factory.llm.providers.gemini_provider import GeminiProvider
    from factory.llm.router import Provider

    settings = Settings(
        gemini_api_key="k",
        anthropic_api_key="k",
        staging_mock_expensive_calls=True,
        workspace_root=tmp_path,
    )

    registry = build_registry(settings)

    assert isinstance(registry.get(Provider.ANTHROPIC), StagingCodeProvider)
    assert isinstance(registry.get(Provider.GEMINI), GeminiProvider)
    assert not isinstance(registry.get(Provider.GEMINI), AnthropicProvider)


def test_the_flag_off_leaves_the_real_anthropic_provider_wired(tmp_path: Path) -> None:
    from factory.llm.providers.anthropic_provider import AnthropicProvider

    settings = Settings(
        gemini_api_key="k",
        anthropic_api_key="k",
        staging_mock_expensive_calls=False,
        workspace_root=tmp_path,
    )

    registry = build_registry(settings)

    assert isinstance(registry.get(Provider.ANTHROPIC), AnthropicProvider)
