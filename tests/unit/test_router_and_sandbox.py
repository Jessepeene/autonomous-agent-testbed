"""Provider routing, failover policy, and the fail-closed sandbox."""

from __future__ import annotations

import pytest

from factory.llm.client import LLMClient, NoProviderAvailable
from factory.llm.providers.base import (
    Provider,
    ProviderRefusal,
    ProviderUnavailable,
    SchemaViolation,
)
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import (
    NO_FAILOVER,
    ROUTING,
    ProviderRegistry,
    may_failover,
    model_for,
    provider_for,
)
from factory.llm.schema import json_schema_for
from factory.sandbox.disabled import DisabledSandbox
from factory.sandbox.runner import SandboxUnavailable, UnsafeWorkspace, resolve_workspace
from factory.settings import Settings, TaskType

# --- routing table ----------------------------------------------------------


def test_every_task_type_is_routed() -> None:
    """A task type with no provider would fail at runtime, not at import."""
    for task_type in TaskType:
        assert task_type in ROUTING, f"{task_type} has no provider"
        assert provider_for(task_type) in (Provider.GEMINI, Provider.ANTHROPIC)


def test_code_tasks_route_to_claude() -> None:
    assert provider_for(TaskType.CODE_GENERATION) is Provider.ANTHROPIC
    assert provider_for(TaskType.CODE_REVIEW) is Provider.ANTHROPIC


def test_general_tasks_route_to_gemini() -> None:
    for task_type in (
        TaskType.ORCHESTRATION,
        TaskType.DISCOVERY,
        TaskType.ANALYSIS,
        TaskType.COMPLIANCE,
        TaskType.MARKETING_COPY,
    ):
        assert provider_for(task_type) is Provider.GEMINI


def test_code_tasks_never_fail_over() -> None:
    assert NO_FAILOVER == {TaskType.CODE_GENERATION, TaskType.CODE_REVIEW}
    assert not may_failover(TaskType.CODE_GENERATION)
    assert may_failover(TaskType.DISCOVERY)


def test_model_ids_come_from_settings(settings: Settings) -> None:
    assert model_for(TaskType.CODE_GENERATION, settings) == settings.model_code
    assert model_for(TaskType.ORCHESTRATION, settings) == settings.model_fast
    assert model_for(TaskType.COMPLIANCE, settings) == settings.model_general


# --- failover policy --------------------------------------------------------


async def test_general_task_fails_over_on_outage(
    settings: Settings, gemini_fake: FakeProvider, anthropic_fake: FakeProvider
) -> None:
    from factory.state.schemas import IdeaBatch

    # Three failures exhausts the primary's retries; only then may it fail over.
    gemini_fake.register(
        IdeaBatch, *[ProviderUnavailable(Provider.GEMINI, "503", status=503) for _ in range(3)]
    )
    anthropic_fake.register(IdeaBatch, IdeaBatch(ideas=[]))
    client = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini_fake, Provider.ANTHROPIC: anthropic_fake}),
        settings=settings,
        backoff_base=0.0,
    )

    result = await client.parse(
        IdeaBatch,
        task_type=TaskType.DISCOVERY,
        system="s",
        prompt="p",
    )
    assert result.ideas == []
    assert anthropic_fake.calls, "failover never reached the alternative provider"


async def test_code_task_does_not_fail_over_to_gemini(
    settings: Settings, gemini_fake: FakeProvider, anthropic_fake: FakeProvider
) -> None:
    """A cross-vendor failover here would change who wrote the shipped code."""
    from factory.state.schemas import ScaffoldPlan

    for _ in range(3):
        anthropic_fake.register(
            ScaffoldPlan, ProviderUnavailable(Provider.ANTHROPIC, "503", status=503)
        )
    client = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini_fake, Provider.ANTHROPIC: anthropic_fake}),
        settings=settings,
    )

    with pytest.raises(ProviderUnavailable):
        await client.parse(ScaffoldPlan, task_type=TaskType.CODE_GENERATION, system="s", prompt="p")
    assert gemini_fake.calls == [], "a code task was served by the wrong provider"


async def test_refusal_is_never_laundered_through_the_other_provider(
    settings: Settings, gemini_fake: FakeProvider, anthropic_fake: FakeProvider
) -> None:
    from factory.state.schemas import IdeaBatch

    gemini_fake.register(IdeaBatch, ProviderRefusal(Provider.GEMINI, "SAFETY", "declined"))
    anthropic_fake.register(IdeaBatch, IdeaBatch(ideas=[]))
    client = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini_fake, Provider.ANTHROPIC: anthropic_fake}),
        settings=settings,
    )

    with pytest.raises(ProviderRefusal):
        await client.parse(IdeaBatch, task_type=TaskType.DISCOVERY, system="s", prompt="p")
    assert anthropic_fake.calls == [], "a policy refusal was retried on another vendor"


async def test_schema_violation_gets_one_repair_attempt(
    settings: Settings, gemini_fake: FakeProvider, anthropic_fake: FakeProvider
) -> None:
    from factory.state.schemas import IdeaBatch

    gemini_fake.register(
        IdeaBatch,
        SchemaViolation("missing field"),
        IdeaBatch(ideas=[]),
    )
    client = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini_fake, Provider.ANTHROPIC: anthropic_fake}),
        settings=settings,
    )
    result = await client.parse(IdeaBatch, task_type=TaskType.DISCOVERY, system="s", prompt="p")
    assert result.ideas == []
    assert len(gemini_fake.calls) == 2
    assert "schema validation" in gemini_fake.calls[1]["prompt"]


async def test_no_available_provider_raises(settings: Settings) -> None:
    from factory.state.schemas import IdeaBatch

    empty = FakeProvider(Provider.GEMINI)
    empty.available_flag = False
    client = LLMClient(ProviderRegistry({Provider.GEMINI: empty}), settings=settings)
    with pytest.raises(NoProviderAvailable):
        await client.parse(IdeaBatch, task_type=TaskType.DISCOVERY, system="s", prompt="p")


# --- schema emission --------------------------------------------------------


def test_json_schema_strips_unsupported_keywords() -> None:
    from factory.state.schemas import OrchestratorDecision

    schema = json_schema_for(OrchestratorDecision)
    rendered = repr(schema)
    for keyword in ("minLength", "maxLength", "pattern", "minimum", "maximum"):
        assert keyword not in rendered
    assert schema["additionalProperties"] is False


# --- sandbox ----------------------------------------------------------------


async def test_disabled_sandbox_always_raises() -> None:
    sandbox = DisabledSandbox()
    assert sandbox.available is False
    with pytest.raises(SandboxUnavailable):
        await sandbox.execute("echo hi", workspace="anything")


@pytest.mark.parametrize(
    "name",
    ["../escape", "/etc", "a/b", "..", ".", "", "with space", "x" * 100],
)
def test_workspace_names_that_could_escape_are_refused(name: str, settings: Settings) -> None:
    with pytest.raises(UnsafeWorkspace):
        resolve_workspace(name, settings)


def test_valid_workspace_resolves_under_root(settings: Settings) -> None:
    path = resolve_workspace("product-abc123", settings)
    assert path.parent == settings.workspace_root.resolve()
