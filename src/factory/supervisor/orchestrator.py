"""Routing brain.

Deterministic guards run *before* the model is consulted, and the model's answer is
validated against the set of engines that are actually eligible. The LLM chooses
among legal options; it never invents one.

The Discovery throttle lives here. Discovery runs on a 6-hour cadence and can
generate ideas far faster than Development can build them, so at capacity the
Supervisor holds new ideas in ``pending`` and redirects the tick to backlog work
rather than letting the queue grow without bound.

Below the throttle, routing is deterministic-first (`factory.supervisor.routing`):
the model is consulted only when its judgement can add something a fixed priority
scan cannot — see that module's docstring for the exact triggers. This turned the
single largest source of LLM calls in the system (up to 5,760/day from the tick loop)
into roughly a 95% reduction, without giving up the model's role in the gaps a
priority table cannot cover.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import role_catalogue
from factory.hitl import interrupts
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.observability import costs
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.schemas import InterruptReason, NextAction, OrchestratorDecision
from factory.supervisor import budget, routing
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """\
You are the Supervisor of an autonomous B2B micro-SaaS factory. You decide which \
engine does work next.

You do not perform work yourself and you do not write prose for a human. You emit a \
single routing decision.

Principles, in priority order:
1. Continue autonomously. Escalation is a last resort, permitted only for a missing \
credential or genuine legal uncertainty.
2. Prefer work that is closest to revenue: shipping a validated idea beats \
discovering another one; fixing a live product beats shipping a new one.
3. Never leave the factory idle. If no revenue-bearing work is ready, choose \
IDLE_BACKLOG so maintenance runs.
4. Respect capacity. If you are told the development pipeline is full, do not route \
more work to it.

Choose target_engine only from the eligible engines you are given. Keep the \
rationale under two sentences.

When work is needed that no fixed engine covers — an open question about the outside \
world, a stubborn failure that needs investigating, a health check on a live URL — \
emit SPAWN_AGENT with an agent_role from this catalogue and one concrete \
agent_objective:

{roles}

Do not invent a role that is not listed. Prefer routing to an engine when one fits; \
spawning is for the gaps.\
"""


def _system_prompt() -> str:
    """Rendered per call, not cached, so a role added to the catalogue is visible on
    the very next tick rather than requiring a process restart."""
    return _SYSTEM_PROMPT_TEMPLATE.format(roles=role_catalogue())


#: Re-exported so `orchestrator.FactorySnapshot` keeps working — the type now lives
#: in `routing.py` because `routing.py` needs it and `orchestrator.py` importing
#: `routing.py` (rather than the reverse) is what keeps `FactoryContext` (which holds
#: a `RoutingMemo`) free of a circular import back to this module.
FactorySnapshot = routing.FactorySnapshot


async def build_snapshot(session: AsyncSession, ctx: FactoryContext) -> FactorySnapshot:
    from sqlalchemy import func, select

    from factory.state.orm import Idea, Interrupt, Product
    from factory.state.schemas import ProductStatus

    pending_by_engine = {
        engine.value: await repository.count_pending_tasks(session, engine)
        for engine in ctx.enabled_engines
    }
    # An approved idea always gets a product in the same compliance pass (see
    # test_approved_idea_creates_a_validating_product_not_a_build), so a bare
    # count of approved ideas is monotonically increasing forever -- it never
    # drops even once every one of them has shipped or been retired, and by
    # itself tells the model nothing about whether Development actually has
    # anything left to do. Excluding the two terminal statuses (nothing further
    # is ever queued for either) is what makes this a live "still needs
    # attention" count rather than a lifetime tally. Observed live: this stale
    # count alone was enough to make the model route to Development on a tick
    # where its queue was provably empty.
    approved = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Idea)
                .join(Product, Product.idea_id == Idea.id)
                .where(
                    Idea.status == "approved",
                    Product.status.notin_([ProductStatus.RETIRED.value, ProductStatus.LIVE.value]),
                )
            )
        ).scalar_one()
    )
    pending_ideas = int(
        (
            await session.execute(
                select(func.count()).select_from(Idea).where(Idea.status == "pending")
            )
        ).scalar_one()
    )
    open_interrupts = int(
        (
            await session.execute(
                select(func.count()).select_from(Interrupt).where(Interrupt.status == "open")
            )
        ).scalar_one()
    )

    pending_dev_retries = (
        await repository.count_pending_dev_retries(session)
        if EngineName.DEVELOPMENT in ctx.enabled_engines
        else 0
    )

    return FactorySnapshot(
        eligible_engines=sorted(e.value for e in ctx.enabled_engines),
        disabled_engines={str(k): v for k, v in ctx.disabled_reasons.items()},
        pending_tasks_total=sum(pending_by_engine.values()),
        pending_by_engine=pending_by_engine,
        active_dev_products=await repository.count_active_dev_tasks(session),
        max_concurrent_builds=ctx.settings.max_concurrent_builds,
        pending_dev_retries=pending_dev_retries,
        approved_ideas_waiting=approved,
        pending_ideas=pending_ideas,
        open_interrupts=open_interrupts,
        spend_limit_cents=await budget.get_spend_limit(session),
        dry_run=ctx.dry_run,
    )


def _hold_discovery(snapshot: FactorySnapshot) -> OrchestratorDecision:
    return OrchestratorDecision(
        next_action=NextAction.HOLD_DISCOVERY,
        target_engine=EngineName.SUPERVISOR,
        tool_call=None,
        rationale=(
            f"Development pipeline at capacity "
            f"({snapshot.active_dev_products}/{snapshot.max_concurrent_builds}); "
            "validated ideas stay queued and the tick does backlog work instead."
        ),
    )


def _idle_backlog(reason: str) -> OrchestratorDecision:
    return OrchestratorDecision(
        next_action=NextAction.IDLE_BACKLOG,
        target_engine=EngineName.SUPERVISOR,
        tool_call=None,
        rationale=reason,
    )


def _budget_paused(spent_usd: float, cap_usd: float) -> OrchestratorDecision:
    return OrchestratorDecision(
        next_action=NextAction.BUDGET_PAUSED,
        target_engine=EngineName.SUPERVISOR,
        tool_call=None,
        rationale=(
            f"Daily LLM spend cap exceeded: ${spent_usd:.2f} of ${cap_usd:.2f} in the "
            "trailing 24h. Every generative task is paused until spend ages out of "
            "the window."
        ),
    )


async def _check_budget(session: AsyncSession, ctx: FactoryContext) -> OrchestratorDecision | None:
    """The financial circuit breaker. Checked before anything else in `decide()` —
    a tripped breaker overrides even revenue-critical routing, because the whole
    point is an unconditional, no-exceptions pause on spend.

    Deterministic and never delegated to the model: an LLM call to decide whether to
    stop making LLM calls is the one place a "just this once" rationalisation would be
    catastrophic. The 24h window is rolling, not calendar-day, so the pause lifts on
    its own as old calls age out — no reset job, no manual unpause.
    """
    cap = ctx.settings.daily_llm_spend_cap_usd
    if cap is None:
        return None
    spent = await costs.total_usd(session, hours=24)
    if spent < cap:
        return None

    day = datetime.now(UTC).date().isoformat()
    await interrupts.request(
        session,
        reason=InterruptReason.BUDGET_EXCEEDED,
        subject=f"Daily LLM spend cap exceeded ({day})",
        detail=(
            f"Aggregate LLM spend over the trailing 24h reached ${spent:.2f}, at or "
            f"above the configured cap of ${cap:.2f}. All generative tasks (Discovery, "
            "Development, Marketing copy, Compliance assessment, and every ephemeral "
            "agent spawn) are paused until spend falls back under the cap. Nothing "
            "needs a reply — this clears automatically as older calls age out of the "
            "24h window. Reply only if you want to raise or disable the cap."
        ),
    )
    log.warning("orchestrator.budget_paused", spent_usd=spent, cap_usd=cap)
    return _budget_paused(spent, cap)


async def decide(session: AsyncSession, ctx: FactoryContext) -> OrchestratorDecision:
    """Pick the next engine to run.

    Deterministic guards first — capacity and emptiness are facts, not judgements,
    and asking a model about them would only add latency and a chance of being wrong.
    Below that, routing is deterministic-first: the model is consulted only when
    `routing.deliberation_reason` finds a reason it can add something a fixed
    priority scan cannot (see that function for the exact triggers).
    """
    paused = await _check_budget(session, ctx)
    if paused is not None:
        return paused

    snapshot = await build_snapshot(session, ctx)

    if snapshot.dev_at_capacity:
        # A pending build/repair task for a product *already* counted in
        # active_dev_products doesn't admit a new product into the pipeline, so it
        # must never be held back by this gate the way a fresh build_mvp for a new
        # product is. Checked first, ahead of even Marketing. Covers two shapes of
        # the same deadlock: a product stuck in PATCHING (observed live -- two
        # products stuck, their patch tasks pending, HOLD_DISCOVERY repeating every
        # tick indefinitely), and a BUILD_MVP that threw partway through and left
        # its product stuck in BUILDING (that status is set before the attempt and
        # never reset on failure) with its retry sitting pending and just as
        # invisible to a check that only ever counted PATCH_PRODUCT.
        if snapshot.pending_dev_retries > 0:
            log.info(
                "orchestrator.capacity_prefers_repair",
                active=snapshot.active_dev_products,
                capacity=snapshot.max_concurrent_builds,
                pending_dev_retries=snapshot.pending_dev_retries,
            )
            return OrchestratorDecision(
                next_action=NextAction.ROUTE,
                target_engine=EngineName.DEVELOPMENT,
                tool_call=None,
                rationale=(
                    "Development pipeline at capacity, but a queued build/repair "
                    "belongs to a product already counted in that capacity -- "
                    "finishing a live product outranks starting a new one."
                ),
            )
        # Distribution, not code generation, is the bottleneck: cycles freed by a
        # full development pipeline go to selling what already exists before they go
        # to generic maintenance. Marketing is checked directly here — bypassing
        # `deterministic_route`'s full priority scan, which would also route to
        # Development or Compliance work this tick must not start — so a capacity
        # tick can still resolve to organic outreach rather than always falling
        # through to backlog.
        if snapshot.pending_by_engine.get(EngineName.MARKETING.value, 0) > 0:
            log.info(
                "orchestrator.dev_capacity_prefers_marketing",
                active=snapshot.active_dev_products,
                capacity=snapshot.max_concurrent_builds,
            )
            return OrchestratorDecision(
                next_action=NextAction.ROUTE,
                target_engine=EngineName.MARKETING,
                tool_call=None,
                rationale=(
                    "Development pipeline at capacity; distribution outranks starting "
                    "another build, and Marketing has queued work."
                ),
            )
        log.info(
            "orchestrator.hold_discovery",
            active=snapshot.active_dev_products,
            capacity=snapshot.max_concurrent_builds,
        )
        return _hold_discovery(snapshot)

    if snapshot.pending_tasks_total == 0:
        return _idle_backlog("No task is ready; running maintenance so the factory is never idle.")

    memo = ctx.routing
    fp = routing.fingerprint(snapshot)
    routing.observe_tick(memo, fp)
    reason = routing.deliberation_reason(snapshot, memo, ctx.settings)

    if reason is None:
        decision = routing.replay(memo, fp, ctx) or routing.deterministic_route(snapshot)
        # `deliberation_reason` already confirmed `deterministic_route` is non-None
        # here (that is exactly what "not ambiguous_route" means), so this is a
        # sanity check, not a real fallback path.
        if decision is None:  # pragma: no cover - see comment above
            decision = _fallback(snapshot)
        log.info(
            "orchestrator.deterministic",
            action=str(decision.next_action),
            engine=str(decision.target_engine),
        )
        return decision

    decision = await _ask_model(ctx, snapshot, reason, memo=memo, fp=fp)
    eligible = {e.value for e in ctx.enabled_engines} | {EngineName.SUPERVISOR.value}
    if decision.target_engine.value not in eligible:
        log.warning(
            "orchestrator.invalid_route",
            proposed=decision.target_engine.value,
            eligible=sorted(eligible),
        )
        return _fallback(snapshot)
    return decision


async def _ask_model(
    ctx: FactoryContext,
    snapshot: FactorySnapshot,
    reason: str,
    *,
    memo: routing.RoutingMemo,
    fp: str,
) -> OrchestratorDecision:
    why = routing.REASON_TEXT.get(reason, reason)
    prompt = (
        f"Why you are being asked this tick: {why}\n\n"
        "Current factory state:\n"
        f"{json.dumps(asdict(snapshot), separators=(',', ':'), sort_keys=True)}\n\n"
        "Emit the routing decision."
    )
    try:
        decision = await ctx.llm.parse(
            OrchestratorDecision,
            task_type=TaskType.ORCHESTRATION,
            system=_system_prompt(),
            prompt=prompt,
            engine=EngineName.SUPERVISOR,
        )
    except LLMError as exc:
        # Routing must survive a provider outage: fall back to the deterministic
        # priority order rather than stalling the whole factory on one bad call. This
        # is not a real model consultation, so the cadence/stall clocks do not reset.
        log.warning("orchestrator.llm_failed", detail=str(exc)[:300])
        return _fallback(snapshot)
    routing.record_decision(memo, decision, fp)
    return decision


def _fallback(snapshot: FactorySnapshot) -> OrchestratorDecision:
    """Deterministic priority order, used when the model is unavailable or wrong."""
    return routing.deterministic_route(snapshot) or _idle_backlog(
        "Deterministic fallback: nothing queued, running maintenance."
    )
