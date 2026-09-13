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
    POC_EXEMPT,
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

# --- POC model override -----------------------------------------------------


def test_poc_override_sends_every_non_exempt_task_type_to_one_model(
    settings: Settings,
) -> None:
    """POC mode runs the factory on one cheap model, because every bug found in two
    days of live debugging was structural -- a cap, an ordering, a dropped payload
    -- and a weak model exposes that class *better* than a strong one, which papers
    over a thin brief by inferring what was meant."""
    poc = settings.model_copy(update={"poc_model_override": "deepseek-flash"})
    for task_type in TaskType:
        if task_type in POC_EXEMPT:
            continue
        assert provider_for(task_type, poc) is Provider.DEEPSEEK
        assert model_for(task_type, poc) == "deepseek-flash"


def test_code_generation_stays_on_claude_even_in_poc_mode(settings: Settings) -> None:
    """The one exemption, and the reason is specific: CODE_GENERATION is the single
    call that produces a whole working MVP in one shot, and Claude is demonstrably
    able to do that. It is also the least informative thing to put on a cheap model
    -- a failed one-shot MVP measures the model's generation ceiling, not the
    factory's plumbing, which is what the POC is for.

    CODE_REVIEW is deliberately not exempt: if the system works as it should, who
    reviews the code should not matter."""
    poc = settings.model_copy(update={"poc_model_override": "deepseek-flash"})
    assert provider_for(TaskType.CODE_GENERATION, poc) is Provider.ANTHROPIC
    assert model_for(TaskType.CODE_GENERATION, poc) == poc.model_code
    assert provider_for(TaskType.CODE_REVIEW, poc) is Provider.DEEPSEEK


def test_clearing_the_poc_override_restores_the_table_exactly(settings: Settings) -> None:
    """The override must be a switch, not a one-way door -- unsetting it has to
    give back the documented routing table, including the Anthropic-only code
    paths, with nothing left behind."""
    poc = settings.model_copy(update={"poc_model_override": "deepseek-flash"})
    assert provider_for(TaskType.CODE_REVIEW, poc) is Provider.DEEPSEEK

    restored = settings.model_copy(update={"poc_model_override": None})
    for task_type in TaskType:
        assert provider_for(task_type, restored) is ROUTING[task_type]


def test_poc_override_does_not_widen_failover(settings: Settings) -> None:
    """NO_FAILOVER exists so code is never *silently* served by a vendor nobody
    chose. POC mode is one explicit vendor chosen by an operator -- it must not
    also make code paths failover-eligible."""
    poc = settings.model_copy(update={"poc_model_override": "deepseek-flash"})
    assert poc.poc_model_override
    for task_type in (TaskType.CODE_GENERATION, TaskType.CODE_REVIEW, TaskType.CODE_REPAIR):
        assert may_failover(task_type) is False


# --- routing table ----------------------------------------------------------


def test_every_task_type_is_routed() -> None:
    """A task type with no provider would fail at runtime, not at import."""
    for task_type in TaskType:
        assert task_type in ROUTING, f"{task_type} has no provider"
        assert provider_for(task_type) in (Provider.GEMINI, Provider.ANTHROPIC, Provider.DEEPSEEK)


def test_code_tasks_route_to_claude() -> None:
    assert provider_for(TaskType.CODE_GENERATION) is Provider.ANTHROPIC
    assert provider_for(TaskType.CODE_REVIEW) is Provider.ANTHROPIC


def test_code_repair_routes_to_deepseek() -> None:
    """Split from CODE_GENERATION: agentic tool-calling repair/healing work
    (WORKSPACE_SURGEON/SEO_OPTIMIZER/REPAIR_ENGINEER) goes through `converse()`,
    which DeepSeek handles -- unlike CODE_GENERATION/CODE_REVIEW's `parse()`-based
    strict JSON-schema output, which it does not reliably produce."""
    assert provider_for(TaskType.CODE_REPAIR) is Provider.DEEPSEEK


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
    assert NO_FAILOVER == {
        TaskType.LANDING_PAGE,
        TaskType.CODE_GENERATION,
        TaskType.CODE_ITERATION,
        TaskType.CODE_REVIEW,
        TaskType.CODE_REPAIR,
    }
    # The property behind the list: anything that writes or judges code that
    # ships is here, so a new code task type cannot be added without deciding
    # this deliberately.
    for task_type in TaskType:
        writes_code = task_type.value.startswith(("code_", "landing_"))
        assert may_failover(task_type) is not writes_code
    assert not may_failover(TaskType.CODE_GENERATION)
    assert not may_failover(TaskType.CODE_REPAIR)
    assert may_failover(TaskType.DISCOVERY)


def test_model_ids_come_from_settings(settings: Settings) -> None:
    assert model_for(TaskType.CODE_GENERATION, settings) == settings.model_code
    assert model_for(TaskType.CODE_REPAIR, settings) == settings.model_deepseek
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
