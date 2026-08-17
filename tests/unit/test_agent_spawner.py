"""Ephemeral sub-agent creation — the Orchestrator's answer to "I need a capability
no fixed engine has."

Three properties matter here, and each has a direct test:

* **The role catalogue is the containment.** A role not in the catalogue cannot be
  spawned, and a role's tool set is fixed by the catalogue entry, not by what the
  caller asks for.
* **Capability gates are checked before anything runs.** A sandbox-needing role with
  no Docker, or a mutating role under FACTORY_DRY_RUN, is refused before a single
  token is spent.
* **Memory closes the loop.** What one spawn concludes is recallable by the next.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import (
    PIPELINE_SCOPED_ROLES,
    ROLES,
    WORKSPACE_SCOPED_ROLES,
    RoleName,
    get_role,
    role_catalogue,
)
from factory.agents.runtime import AgentResult
from factory.agents.spawner import RoleNotPermitted, SpawnRequest, can_spawn, resolve_role, spawn
from factory.llm.providers.base import AgentTurn, Provider, Usage
from factory.llm.providers.fake import FakeProvider
from factory.memory import store as memory_store
from factory.settings import EngineName
from factory.supervisor.context import FactoryContext


def _completion(text: str) -> AgentTurn:
    return AgentTurn(
        text=text,
        tool_calls=[],
        stop_reason="end_turn",
        provider=Provider.GEMINI,
        model="gemini-2.5-flash",
        usage=Usage(input_tokens=5, output_tokens=5),
    )


# --- role resolution ---------------------------------------------------------


def test_every_declared_role_resolves() -> None:
    for name in RoleName:
        role = get_role(name)
        assert role.name is name


def test_resolve_role_accepts_the_string_form() -> None:
    role = resolve_role("analyst")
    assert role.name is RoleName.ANALYST


def test_unknown_role_is_not_permitted() -> None:
    with pytest.raises(RoleNotPermitted):
        resolve_role("prompt_engineer")


def test_role_catalogue_lists_every_orchestrator_selectable_role_by_name() -> None:
    rendered = role_catalogue()
    for name in RoleName:
        if name in WORKSPACE_SCOPED_ROLES or name in PIPELINE_SCOPED_ROLES:
            continue
        assert name.value in rendered


def test_role_catalogue_hides_workspace_scoped_roles_from_the_orchestrator() -> None:
    """This catalogue is literally the menu the Orchestrator picks from, so anything
    listed here is something it can spawn with a model-chosen objective, as
    SUPERVISOR, outside any product lock and outside heal accounting. A workspace
    surgeon is only safe inside `_patch`, which pins it to one product, holds the
    lock, and can roll its edits back -- none of which the Orchestrator provides."""
    rendered = role_catalogue()
    assert WORKSPACE_SCOPED_ROLES, "the exclusion set must not be silently emptied"
    for name in WORKSPACE_SCOPED_ROLES:
        assert name.value not in rendered


def test_role_catalogue_hides_pipeline_scoped_roles_from_the_orchestrator() -> None:
    """DISCOVERY_SCOUT is only ever spawned from `engines/discovery/scout.py`, once
    per Discovery cycle, with a fixed objective and that cycle's own `task_id` --
    not something the Orchestrator should hand an arbitrary objective."""
    rendered = role_catalogue()
    assert PIPELINE_SCOPED_ROLES, "the exclusion set must not be silently emptied"
    for name in PIPELINE_SCOPED_ROLES:
        assert name.value not in rendered


# --- capability gating: checked before anything runs -------------------------


def test_sandbox_role_is_refused_without_docker(ctx: FactoryContext) -> None:
    assert ctx.docker_available is False
    role = get_role(RoleName.REPAIR_ENGINEER)
    permitted, reason = can_spawn(ctx, role)
    assert permitted is False
    assert "sandbox" in reason


def test_sandbox_role_is_permitted_with_docker(ctx: FactoryContext) -> None:
    ctx.docker_available = True
    ctx.settings.factory_dry_run = False
    role = get_role(RoleName.REPAIR_ENGINEER)
    permitted, _ = can_spawn(ctx, role)
    assert permitted is True


def test_mutating_role_is_refused_under_dry_run(ctx: FactoryContext) -> None:
    """Synthesised because no catalogue role is mutating without also needing a
    sandbox; this isolates the dry-run gate specifically."""
    assert ctx.dry_run is True
    synthetic = replace(get_role(RoleName.ANALYST), tool_categories=("http",), mutating=True)
    permitted, reason = can_spawn(ctx, synthetic)
    assert permitted is False
    assert "DRY_RUN" in reason


def test_non_mutating_role_ignores_dry_run(ctx: FactoryContext) -> None:
    assert ctx.dry_run is True
    role = get_role(RoleName.ANALYST)
    permitted, _ = can_spawn(ctx, role)
    assert permitted is True


def test_every_role_declares_a_context_budget() -> None:
    """A role with no `max_context_chars` would be silently unbounded again --
    exactly the gap Phase 8 closes. Every declared role must set one, whether or
    not it goes through `spawn()` (repair.py reads WORKSPACE_SURGEON/SEO_OPTIMIZER's
    directly)."""
    for role in ROLES.values():
        assert role.max_context_chars is not None, role.name


def test_cost_cap_is_set_for_every_role_spawn_reaches_directly() -> None:
    """WORKSPACE_SURGEON/SEO_OPTIMIZER are the only two roles `spawn()` never
    reaches (see WORKSPACE_SCOPED_ROLES) -- `repair.py` applies its own
    settings-based `repair_agent_cost_cap_usd` for them instead, so their role-
    level field stays `None` on purpose. Every role `spawn()` can actually invoke
    must declare its own, or it is silently uncapped."""
    for name, role in ROLES.items():
        if name in WORKSPACE_SCOPED_ROLES:
            assert role.cost_cap_usd is None, name
        else:
            assert role.cost_cap_usd is not None, name


def test_read_only_roles_are_all_declared_non_mutating() -> None:
    """A sanity check on the catalogue itself: exactly the roles that can act on
    the world (edit live code, start a real AB test, enqueue a real patch, raise
    a real human-approval request) are flagged mutating."""
    mutating = {name for name, role in ROLES.items() if role.mutating}
    assert mutating == {RoleName.REPAIR_ENGINEER, RoleName.GROWTH_ANALYST, RoleName.CHANNEL_SCOUT}


# --- spawn(): refusal never touches the provider ------------------------------


async def test_refused_spawn_never_calls_the_model(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    # Deliberately no queued turn: if the provider were called, FakeProvider raises.
    result = await spawn(
        ctx,
        None,
        SpawnRequest(role=RoleName.REPAIR_ENGINEER, objective="fix the build"),
    )
    assert result.stopped_because == "not_permitted"
    assert result.steps == 0
    assert gemini_fake.conversations == []


# --- spawn(): the happy path, with and without memory -------------------------


async def test_spawn_without_a_session_skips_memory_entirely(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    gemini_fake.register_turns(_completion("Nothing looks degraded."))
    result = await spawn(
        ctx, None, SpawnRequest(role=RoleName.AUDITOR, objective="check the product")
    )
    assert result.succeeded
    assert result.answer == "Nothing looks degraded."


async def test_spawn_writes_a_memory_on_success(
    ctx: FactoryContext, gemini_fake: FakeProvider, memory_session: AsyncSession
) -> None:
    gemini_fake.register_turns(_completion("The checkout endpoint returns 503."))
    await spawn(
        ctx,
        memory_session,
        SpawnRequest(
            role=RoleName.AUDITOR,
            objective="check the product",
            engine=EngineName.MARKETING,
            product_id="prod-1",
        ),
    )
    await memory_session.commit()

    hits = await memory_store.recall(
        memory_session,
        query="checkout endpoint 503",
        scopes=memory_store.scopes_for(product_id="prod-1"),
    )
    assert len(hits) == 1
    assert "503" in hits[0].content


async def test_spawn_recalls_prior_memory_into_the_objective_context(
    ctx: FactoryContext, gemini_fake: FakeProvider, memory_session: AsyncSession
) -> None:
    """What one spawn learns must reach the next spawn's prompt, not just the DB."""
    await memory_store.remember(
        memory_session,
        content=(
            "This product's health check flakes on cold start; retry once before concluding down."
        ),
        scope=memory_store.scope_for(product_id="prod-1"),
        origin="test",
    )
    await memory_session.commit()

    gemini_fake.register_turns(_completion("Retried once; the product is healthy."))
    await spawn(
        ctx,
        memory_session,
        SpawnRequest(
            role=RoleName.AUDITOR,
            objective="check the product",
            product_id="prod-1",
        ),
    )

    sent_prompt = gemini_fake.conversations[0]["turns"][0].text
    assert "flakes on cold start" in sent_prompt


async def test_spawn_does_not_write_a_memory_on_failure(
    ctx: FactoryContext,
    gemini_fake: FakeProvider,
    anthropic_fake: FakeProvider,
    memory_session: AsyncSession,
) -> None:
    from factory.llm.providers.base import ProviderUnavailable

    # ANALYST's task type may fail over to Anthropic; taking it out of service means
    # this failure is a clean outage rather than needing queued failures on both.
    anthropic_fake.available_flag = False
    for _ in range(3):  # LLMClient retries a transport failure 3x before giving up.
        gemini_fake.register_turns(ProviderUnavailable(Provider.GEMINI, "503", status=503))
    result = await spawn(
        ctx, memory_session, SpawnRequest(role=RoleName.ANALYST, objective="anything")
    )
    await memory_session.commit()

    assert not result.succeeded
    assert await memory_store.count(memory_session) == 0


async def test_spawned_agent_only_receives_its_roles_tools(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    """AUDITOR is restricted to the 'http' category. `run_in_sandbox` and
    `github_search_issues` exist in the real registry and must still never be
    offered — the allowlist is the role's tool_categories, not "everything
    currently unlocked by credentials"."""
    from factory.tools import builtin  # noqa: F401 - import populates REGISTRY

    gemini_fake.register_turns(_completion("Nothing looks degraded."))
    await spawn(ctx, None, SpawnRequest(role=RoleName.AUDITOR, objective="check the product"))

    tools_sent = gemini_fake.conversations[0]["tools"]
    tool_names = {t["name"] for t in tools_sent}
    assert tool_names == {"http_get"}
    assert "run_in_sandbox" not in tool_names
    assert "github_search_issues" not in tool_names


async def test_growth_analyst_cannot_reach_channel_scouts_proposal_tool(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    """growth_read/growth_actions and channel_scout are deliberately separate
    categories -- GROWTH_ANALYST must never be able to raise a channel-approval
    interrupt, and CHANNEL_SCOUT (tested below) must never be able to start an AB
    test or request a patch."""
    from factory.tools import growth  # noqa: F401 - import populates REGISTRY

    ctx.settings.factory_dry_run = False
    gemini_fake.register_turns(_completion("No action needed this week."))
    await spawn(ctx, None, SpawnRequest(role=RoleName.GROWTH_ANALYST, objective="review product X"))

    tool_names = {t["name"] for t in gemini_fake.conversations[0]["tools"]}
    assert tool_names == {
        "read_product_growth",
        "list_products_needing_growth_review",
        "start_ab_test_action",
        "request_seo_pass_action",
    }
    assert "propose_channel" not in tool_names


async def test_channel_scout_cannot_reach_growth_analysts_action_tools(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    from factory.tools import growth  # noqa: F401 - import populates REGISTRY

    ctx.settings.factory_dry_run = False
    gemini_fake.register_turns(_completion("Nothing worth proposing yet."))
    await spawn(
        ctx, None, SpawnRequest(role=RoleName.CHANNEL_SCOUT, objective="find a channel for X")
    )

    tool_names = {t["name"] for t in gemini_fake.conversations[0]["tools"]}
    assert tool_names == {
        "http_get",
        "read_product_growth",
        "list_products_needing_growth_review",
        "recent_channel_experiments",
        "propose_channel",
    }
    assert "start_ab_test_action" not in tool_names
    assert "request_seo_pass_action" not in tool_names


# --- spawn(): the role's budgets actually reach the runtime -------------------


async def test_spawn_passes_the_roles_cost_cap_and_context_budget_to_the_runtime(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap this closes: `spawn()` previously passed neither `cost_cap_usd` nor
    `max_context_chars` for any of the roles it reaches -- only `repair.py`'s
    separate direct call site enforced a cost cap, for two different roles, and
    nothing anywhere enforced a context budget."""
    from factory.agents import spawner as spawner_module

    ctx.settings.factory_dry_run = False  # CHANNEL_SCOUT is mutating
    captured: dict[str, object] = {}

    async def fake_run(self: object, **kwargs: object) -> AgentResult:
        captured.update(kwargs)
        return AgentResult(objective=str(kwargs.get("objective")), answer="ok", steps=1)

    monkeypatch.setattr(spawner_module.AgentRuntime, "run", fake_run)

    role = get_role(RoleName.CHANNEL_SCOUT)
    await spawn(ctx, None, SpawnRequest(role=RoleName.CHANNEL_SCOUT, objective="find a channel"))

    assert captured["cost_cap_usd"] == role.cost_cap_usd
    assert captured["max_context_chars"] == role.max_context_chars
    # CHANNEL_SCOUT is ANALYSIS, not in NO_FAILOVER -- no single model is the
    # deterministic answer to "what served this call," so cost_cap is priced
    # against `_spent_usd`'s generic default rather than a specific model name.
    assert captured["cost_model"] is None


async def test_spawn_prices_a_code_generation_roles_cost_cap_against_model_code(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPAIR_ENGINEER is CODE_GENERATION, which `router.NO_FAILOVER` always routes
    to `model_code` deterministically -- unlike the ANALYSIS-type roles, this one
    has an exact model to price its cost cap against."""
    from factory.agents import spawner as spawner_module

    ctx.docker_available = True
    ctx.settings.factory_dry_run = False
    captured: dict[str, object] = {}

    async def fake_run(self: object, **kwargs: object) -> AgentResult:
        captured.update(kwargs)
        return AgentResult(objective=str(kwargs.get("objective")), answer="ok", steps=1)

    monkeypatch.setattr(spawner_module.AgentRuntime, "run", fake_run)

    await spawn(ctx, None, SpawnRequest(role=RoleName.REPAIR_ENGINEER, objective="fix the build"))

    assert captured["cost_model"] == ctx.settings.model_code


async def test_discovery_scout_only_reaches_http_and_submit_signal(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    """DISCOVERY_SCOUT is free to roam the web (`http`) and report back
    (`discovery_scout`'s `submit_signal`) -- nothing else, and in particular none
    of the growth tools it happens to share a spawn mechanism with."""
    from factory.tools import discovery  # noqa: F401 - import populates REGISTRY

    gemini_fake.register_turns(_completion("Found nothing worth reporting this run."))
    await spawn(
        ctx, None, SpawnRequest(role=RoleName.DISCOVERY_SCOUT, objective="find market signals")
    )

    tool_names = {t["name"] for t in gemini_fake.conversations[0]["tools"]}
    assert tool_names == {"http_get", "submit_signal"}
