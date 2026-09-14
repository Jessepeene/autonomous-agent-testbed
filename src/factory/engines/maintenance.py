"""Maintenance runner — the never-idle work.

This used to ask a model for a report and log it, which satisfied "never idle" in the
letter and not at all in the spirit: nothing was investigated and nothing changed.

Now each catalogue item spawns an ephemeral agent with real tools, and whatever it
concludes is written to memory so the next run starts from it rather than rediscovering
it. Items that map to concrete follow-up work enqueue that work as tasks.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Product, Task
from factory.state.schemas import ProductStatus, TaskKind, TaskStatus
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: Which catalogue item gets which capability envelope. An item not listed here runs
#: as an analyst, which is the read-only default. "audit-live-products" is not
#: listed: it is fully deterministic now (see `_audit_live_products`), not an
#: agent spawn, so it never reaches this lookup.
ROLE_BY_SLUG: dict[str, RoleName] = {
    "triage-escalated-tasks": RoleName.ANALYST,
    "reduce-llm-cost": RoleName.ANALYST,
    "improve-test-coverage": RoleName.REPAIR_ENGINEER,
    "refine-agent-prompts": RoleName.ANALYST,
    "refresh-market-signals": RoleName.RESEARCHER,
}

_HEALTH_CHECK_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


async def run(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    slug = str(task.payload.get("slug", "maintenance"))

    # These four are either fully deterministic or need the two-step structured
    # extraction shape from `learning.maintenance` rather than the generic
    # prose-`_act_on()` path below, so they're handled before the generic agent spawn.
    if slug == "propose-prompt-mutation":
        from factory.learning.maintenance import propose_mutations

        return await propose_mutations(ctx, session, task)
    if slug == "evaluate-tunable-trials":
        from factory.learning.trials import promote_and_evaluate

        return await promote_and_evaluate(session)
    if slug == "reconcile-outreach-conversions":
        from factory.engines.marketing.attribution import reconcile

        report = await reconcile(session)
        return (
            f"marketing/attribution: {report.attributed}/{report.checked} attributed, "
            f"{report.identity_episodes} identity episode(s) recorded"
        )
    if slug == "prune-memory":
        from factory.memory.store import prune as prune_memory

        prune_report = await prune_memory(session)
        return (
            f"memory/prune: {prune_report.pruned} evicted across "
            f"{prune_report.scopes_checked} scope(s)"
        )
    if slug == "remind-stale-interrupts":
        from datetime import timedelta

        from factory.hitl.interrupts import remind_stale

        threshold = timedelta(hours=ctx.settings.stale_interrupt_reminder_hours)
        count = await remind_stale(session, ctx.notifier, older_than=threshold)
        return f"hitl/remind: {count} stale interrupt(s) reminded"
    if slug == "audit-snapshot-integrity":
        from factory.supervisor.audit import (
            check_approved_idea_orphans,
            check_business_health,
            check_pipeline_completeness,
            check_scoring_contract_drift,
        )

        count = await check_approved_idea_orphans(session)
        drift = await check_scoring_contract_drift(session)
        # Runs before `check_business_health` so its findings ride out on that
        # function's single interrupt rather than raising a competing one. Several
        # unrelated-looking oddities on the same day read better as one report.
        pipeline_gaps = await check_pipeline_completeness(session)
        concerns = await check_business_health(session, extra_concerns=pipeline_gaps)
        return (
            f"supervisor/audit: {count} orphaned approved idea(s) found, "
            f"scoring contract drift: {drift}, {len(concerns)} business-health concern(s) "
            f"({len(pipeline_gaps)} pipeline gap(s))"
        )
    if slug == "review-churn":
        from factory.engines.finance.churn import review_churn

        return await review_churn(ctx, session, task)
    if slug == "audit-live-products":
        # Deterministic, not an agent spawn (Tier 0 finding #10, system-wide
        # verification plan): a real HTTP check against the product's own
        # deploy_url is strictly cheaper and strictly more reliable than an
        # AUDITOR agent investigating and then string-matching its prose for
        # "degraded"/"down"/"5xx" -- and removes an LLM call from this path
        # entirely rather than adding one.
        return await _audit_live_products(ctx, session)
    if slug == "expire-unconfirmed-signals":
        from datetime import timedelta

        signals_expired = await repository.expire_unconfirmed_validation_signals(
            session, older_than=timedelta(hours=ctx.settings.validation_confirmation_ttl_hours)
        )
        buckets_purged = await repository.purge_rate_limit_buckets(
            session, older_than=timedelta(hours=1)
        )
        return (
            f"signals/expire: {signals_expired} unconfirmed signal(s) deleted, "
            f"{buckets_purged} rate-limit bucket(s) purged"
        )

    objective = str(task.payload.get("objective", "Improve the factory."))
    role = ROLE_BY_SLUG.get(slug, RoleName.ANALYST)

    context = await _gather_context(ctx, session, slug)
    result = await spawn(
        ctx,
        session,
        SpawnRequest(
            role=role,
            objective=objective,
            context=context,
            engine=EngineName.SUPERVISOR,
            task_id=task.id,
        ),
    )

    if not result.succeeded:
        log.info("maintenance.incomplete", slug=slug, stopped=result.stopped_because)
        return f"maintenance/{slug}: {result.summary()}"

    log.info("maintenance.done", slug=slug, role=role.value, steps=result.steps)
    return f"maintenance/{slug}: {result.summary()}"


async def _gather_context(ctx: FactoryContext, session: AsyncSession, slug: str) -> str:
    """Hand the agent the state it needs, so it does not have to guess or ask."""
    blocks: list[str] = [
        f"Factory mode: {'dry-run' if ctx.dry_run else 'live'}.",
        f"Enabled engines: {sorted(e.value for e in ctx.enabled_engines)}.",
        f"Disabled engines: {ctx.disabled_reasons or 'none'}.",
    ]

    if slug == "triage-escalated-tasks":
        stmt = (
            select(Task)
            .where(Task.status == TaskStatus.ESCALATED.value)
            .order_by(Task.updated_at.desc())
            .limit(10)
        )
        escalated = (await session.execute(stmt)).scalars().all()
        if escalated:
            blocks.append(
                "Escalated tasks:\n"
                + "\n".join(
                    f"- {t.id} [{t.kind}/{t.engine}] after {t.attempt_count} attempts: "
                    f"{(t.last_error or 'no error recorded')[:300]}"
                    for t in escalated
                )
            )
        else:
            blocks.append("No tasks are currently escalated.")

    elif slug == "reduce-llm-cost":
        from factory.observability.costs import spend_by_model

        spend = await spend_by_model(session, hours=168)
        if spend:
            blocks.append(
                "LLM spend, last 7 days:\n"
                + "\n".join(
                    f"- {r.provider}/{r.model}: {r.calls} calls, "
                    f"{r.input_tokens} in / {r.output_tokens} out, ${r.usd:.4f}"
                    for r in spend
                )
            )
        else:
            blocks.append("No model calls recorded yet; report that and stop.")

    return "\n\n".join(blocks)


async def check_deploy_url(url: str) -> tuple[str | None, str]:
    """``(degraded_reason, body)`` -- reason is None if the product answers
    healthily. A real request, not a model's account of one -- see
    `_audit_live_products`'s own docstring for why this replaced an AUDITOR
    agent spawn entirely. The body is returned alongside the uptime verdict
    (rather than fetched a second time) so the same request can also back the
    content-freshness check (Tier 0 finding #0b) -- one fetch, two purposes.

    Public (not `_`-prefixed): also reused by `development/design_brief.py`
    to fetch a live product's real rendered page rather than re-implementing
    the same fetch-and-classify logic a second time.
    """
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_CHECK_TIMEOUT) as client:
            response = await client.get(url, follow_redirects=True)
    except httpx.TimeoutException:
        return "request timed out", ""
    except httpx.HTTPError as exc:
        return f"request failed: {exc}", ""
    if response.status_code >= 500:
        return f"HTTP {response.status_code}", ""
    return None, response.text


async def _content_freshness_issue(
    ctx: FactoryContext, *, deploy_url: str, body: str, task_id: str | None
) -> str | None:
    """Tier 0 finding #0b: nothing else ever looks at a LIVE product's actual
    content again after it ships -- a year that was correct at launch goes
    stale the moment the calendar turns, with no backstop against a
    generation that only partially followed the "never hardcode a year"
    instruction. Reuses the QUALITY reviewer from Bug Council (finding #0a)
    against the real fetched page rather than inventing a second mechanism.
    Returns a human-readable summary of what's stale, or None if it still
    looks right.
    """
    if not body.strip():
        return None
    from factory.engines.development import bug_council

    try:
        verdict = await bug_council.review_live_content(
            ctx, {f"live homepage ({deploy_url})": body}, task_id=task_id
        )
    except LLMError as exc:
        # A provider outage here must not itself look like a content
        # problem -- silently skip this pass rather than queue a patch task
        # over an infrastructure hiccup.
        log.warning("maintenance.content_freshness_check_failed", detail=str(exc)[:200])
        return None
    if verdict.approved:
        return None
    findings = "; ".join(f"{f.summary}" for f in verdict.findings[:5])
    return findings or "the quality reviewer flagged this page without a specific finding"


async def _audit_live_products(ctx: FactoryContext, session: AsyncSession) -> str:
    """Deterministic replacement for the AUDITOR-agent-spawn path (Tier 0
    finding #10, system-wide verification plan). The old flow asked an agent
    to investigate and then string-matched its free-text answer for
    "degraded"/"down"/"5xx" -- unreliable in both directions: a hallucinated
    "looks fine" left a real outage undetected (this was the *only* periodic
    check a live product got), and a hallucinated "returned 503" burned a real,
    costed repair cycle on a healthy product. A plain HTTP GET against the
    product's own `deploy_url` is strictly cheaper and strictly more reliable
    than either failure mode for the uptime question, and needs no LLM call
    at all -- see `_content_freshness_issue` for the one, deliberate LLM call
    this pass still makes, against the real fetched body, for the separate
    "does this still look right" question a raw status code can't answer.
    """
    stmt = select(Product).where(Product.status == ProductStatus.LIVE.value).limit(50)
    live = (await session.execute(stmt)).scalars().all()
    if not live:
        return "maintenance/audit-live-products: no live products"

    checked = 0
    degraded = 0
    for product in live:
        if not product.deploy_url:
            continue
        checked += 1
        reason, body = await check_deploy_url(product.deploy_url)
        stale = (
            None
            if reason is not None
            else await _content_freshness_issue(
                ctx, deploy_url=product.deploy_url, body=body, task_id=None
            )
        )
        if reason is None and stale is None:
            continue
        degraded += 1
        healing = (
            f"A direct health check of {product.deploy_url} found it degraded: {reason}."
            if reason is not None
            else (
                f"A content-freshness review of {product.deploy_url} found it stale or "
                f"unfinished-looking: {stale}."
            )
        )
        task_id = await repository.enqueue_task(
            session,
            kind=TaskKind.PATCH_PRODUCT,
            engine=EngineName.DEVELOPMENT,
            idempotency_key=f"patch:{product.id}:audit:{product.updated_at:%Y%m%d%H}",
            payload={
                "product_id": product.id,
                "idea_id": product.idea_id,
                "healing": healing,
            },
            product_id=product.id,
            priority=30,
        )
        if task_id:
            await memory_store.remember(
                session,
                content=(
                    f"Product {product.name} failed a direct health/content check "
                    f"({reason or stale}) and a patch was queued."
                ),
                kind=memory_store.MemoryKind.FAILURE,
                scope=memory_store.scope_for(product_id=product.id),
                origin="maintenance:audit",
            )
    return f"maintenance/audit-live-products: {checked} checked, {degraded} degraded"
