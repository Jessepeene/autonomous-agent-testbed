"""MCP tool registry filtering, engine wiring, and the maintenance catalogue."""

from __future__ import annotations

import subprocess
import sys

import pytest

from factory.engines.base import _MODULES, get_runner
from factory.settings import EngineName, Settings
from factory.supervisor.backlog import CATALOGUE, _bucket
from factory.tools.registry import (
    ToolDescriptor,
    ToolRegistry,
    anthropic_payload,
    gemini_payload,
    mcp_payload,
)

SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
    "required": ["q"],
}


async def _noop(**_: object) -> str:
    return "ok"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDescriptor(
            name="browser_open",
            description="Open a page",
            input_schema=SCHEMA,
            category="browser",
            handler=_noop,
        )
    )
    registry.register(
        ToolDescriptor(
            name="git_clone",
            description="Clone a repo",
            input_schema=SCHEMA,
            category="git",
            handler=_noop,
        )
    )
    registry.register(
        ToolDescriptor(
            name="sandbox_run",
            description="Run a command",
            input_schema=SCHEMA,
            category="sandbox",
            handler=_noop,
            requires_sandbox=True,
        )
    )
    registry.register(
        ToolDescriptor(
            name="stripe_lookup",
            description="Look up a charge",
            input_schema=SCHEMA,
            category="finance",
            handler=_noop,
            required_credential="STRIPE_API_KEY",
        )
    )
    return registry


def test_category_filter_returns_only_requested_categories(settings: Settings) -> None:
    tools = _registry().get_mcp_tools(
        category=["browser", "git"], settings=settings, docker_available=True
    )
    assert [t.name for t in tools] == ["browser_open", "git_clone"]


def test_sandbox_tools_are_withheld_without_docker(settings: Settings) -> None:
    """Offering a tool that cannot run wastes tokens and then fails mid-task."""
    tools = _registry().get_mcp_tools(
        category=["sandbox"], settings=settings, docker_available=False
    )
    assert tools == []
    tools = _registry().get_mcp_tools(
        category=["sandbox"], settings=settings, docker_available=True
    )
    assert [t.name for t in tools] == ["sandbox_run"]


def test_tools_needing_a_missing_credential_are_withheld(settings: Settings) -> None:
    assert settings.secret("STRIPE_API_KEY") is None
    tools = _registry().get_mcp_tools(
        category=["finance"], settings=settings, docker_available=True
    )
    assert tools == []

    with_stripe = settings.model_copy(update={"stripe_api_key": "sk_test_x"})
    tools = _registry().get_mcp_tools(
        category=["finance"], settings=with_stripe, docker_available=True
    )
    assert [t.name for t in tools] == ["stripe_lookup"]


def test_no_category_filter_returns_everything_usable(settings: Settings) -> None:
    tools = _registry().get_mcp_tools(settings=settings, docker_available=True)
    assert {t.name for t in tools} == {"browser_open", "git_clone", "sandbox_run"}


def test_same_descriptor_renders_for_both_providers(settings: Settings) -> None:
    tools = _registry().get_mcp_tools(
        category=["browser"], settings=settings, docker_available=True
    )
    mcp = mcp_payload(tools)[0]
    anthropic = anthropic_payload(tools)[0]
    gemini = gemini_payload(tools)[0]

    assert mcp["inputSchema"] == SCHEMA
    assert anthropic["input_schema"]["additionalProperties"] is False
    assert anthropic["strict"] is True
    assert gemini["parameters"] == SCHEMA
    assert mcp["name"] == anthropic["name"] == gemini["name"] == "browser_open"


def test_duplicate_registration_is_rejected() -> None:
    registry = _registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(
            ToolDescriptor(
                name="git_clone",
                description="dup",
                input_schema=SCHEMA,
                category="git",
                handler=_noop,
            )
        )


def test_builtin_tools_register_cleanly() -> None:
    from factory.tools import builtin  # noqa: F401 - import populates REGISTRY
    from factory.tools.registry import REGISTRY

    names = {t.name for t in REGISTRY.all()}
    assert {"http_get", "run_in_sandbox", "github_search_issues"} <= names


def test_the_tool_registry_is_populated_without_an_explicit_builtin_import() -> None:
    """Regression guard for a P0 bug: `factory.tools.builtin` was previously never
    imported by any production code path (only by tests, which explicitly imported it
    themselves and so never noticed). At runtime `REGISTRY` was empty, `get_mcp_tools()`
    returned `[]` for every role, and every spawned agent exited at step 1 with no
    tools at all. `factory/tools/__init__.py` now imports `builtin` for its side
    effect, so importing anything under the `factory.tools` package is enough. This
    test proves that from a clean interpreter — with no test file's own `import
    builtin` able to mask a regression — by running in a fresh subprocess."""
    script = (
        "import factory.agents.runtime as runtime\n"
        "names = {t.name for t in runtime.REGISTRY.all()}\n"
        "assert {'http_get', 'run_in_sandbox'} <= names, names\n"
    )
    subprocess.run(  # noqa: S603 - fixed script, no untrusted input
        [sys.executable, "-c", script], check=True, timeout=30
    )


# --- engine wiring ----------------------------------------------------------


@pytest.mark.parametrize("engine", list(EngineName))
def test_every_engine_has_an_importable_runner(engine: EngineName) -> None:
    assert engine in _MODULES
    runner = get_runner(engine)
    assert callable(runner)


# --- backlog ----------------------------------------------------------------


def test_catalogue_slugs_are_unique() -> None:
    slugs = [item.slug for item in CATALOGUE]
    assert len(slugs) == len(set(slugs))


def test_bucket_is_stable_within_a_window_and_changes_across_them() -> None:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    assert _bucket(6, now) == _bucket(6, now + timedelta(hours=1))
    assert _bucket(6, now) != _bucket(6, now + timedelta(hours=7))
