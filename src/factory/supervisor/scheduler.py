"""The run loop.

Housekeeping runs before every tick, in this order and for these reasons:

1. **Reclaim expired leases** — a worker that died mid-task left its work claimed;
   nothing else can pick it up until the lease is returned.
2. **Purge expired product locks** — otherwise a crash during a patch would pause
   that product's paid marketing permanently.
3. **Deliver queued escalations** — including any raised while Telegram was
   unconfigured, so fixing the token flushes the whole backlog.
4. **Collect human replies** — resolve interrupts before deciding what to do next.
5. **Sync campaign decisions** — turn a resolved campaign-approval interrupt into
   the cohort's real approved/rejected status, so a pending `SEND_OUTREACH` task
   sees the decision the next time it is attempted.
5b. **Sync channel decisions** — same idea, for a CHANNEL_SCOUT proposal.
6. **Sync deliverability freeze clearance** — same idea, for the spam-complaint
   circuit breaker: only an explicit RESUME reply lifts it.
7. **Schedule Discovery** — on its cadence, and only when Development has headroom.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from factory.engines.marketing import campaign, channel, deliverability
from factory.hitl import interrupts
from factory.llm import health as llm_health
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.db import session_scope
from factory.state.orm import new_id
from factory.state.schemas import InterruptReason, TaskKind
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)


def _bucket(now: datetime, seconds: int) -> str:
    """Collapse time into windows so one task exists per window, not per tick."""
    return str(int(now.timestamp()) // max(seconds, 1))


class Scheduler:
    def __init__(self, ctx: FactoryContext, graph: Any) -> None:
        self._ctx = ctx
        self._graph = graph
        self._tick = 0

    async def housekeeping(self) -> None:
        ctx = self._ctx
        async with session_scope(ctx.settings) as session:
            reclaimed = await repository.reclaim_expired_leases(session)
            purged = await repository.purge_expired_locks(session)
            if reclaimed or purged:
                log.info("housekeeping.recovered", tasks=reclaimed, locks=purged)

            await interrupts.deliver_pending(session, ctx.notifier)
            await interrupts.collect_replies(session, ctx.notifier)
            await campaign.sync_decisions(session)
            await channel.sync_decisions(session)
            await deliverability.sync_freeze_clearance(session)
            await self._refresh_provider_health(session)
            await self._schedule_discovery(session)
            await self._schedule_periodic(session)

    async def _refresh_provider_health(self, session: Any) -> None:
        """Trip or clear the per-provider circuit breaker, and tell a human once.

        Same shape as `deliverability.sync_freeze_clearance` above: a standing
        state that pauses a class of work, recomputed every tick from real
        recorded outcomes rather than held in a state machine that can drift.
        Clearing needs no action at all -- once real calls succeed again, the
        derived query simply stops reporting the provider (see `llm/health.py`).
        """
        ctx = self._ctx
        try:
            tripped = await llm_health.tripped_providers(session)
        except Exception as exc:  # never let an observability query stop a tick
            log.warning("provider_health.check_failed", detail=str(exc)[:200])
            return

        previous = set(ctx.llm.tripped_providers)
        ctx.llm.tripped_providers = set(tripped)

        for provider in sorted(set(tripped) - previous):
            log.warning(
                "provider_health.tripped", provider=provider.value, reason=tripped[provider]
            )
            # One interrupt per outage, not one per blocked task: `open_interrupt`
            # dedups on (reason, subject, status), and the subject is keyed on the
            # provider rather than on whatever task happened to notice -- the exact
            # distinction BUG_LOG #20 was about.
            await interrupts.request(
                session,
                reason=InterruptReason.PROVIDER_OUTAGE,
                subject=f"{provider.value} is in a sustained outage",
                detail=(
                    f"{provider.value}: {tripped[provider]}.\n\n"
                    "Work that depends on this provider is paused rather than retried "
                    "-- no further calls will be made to it until it recovers, so this "
                    "is not costing anything. Tasks routed to it are deferred, not "
                    "failed, and resume automatically once it works again.\n\n"
                    "This is usually billing (an exhausted balance or an expired card) "
                    "or a revoked key."
                ),
                headline="\U0001f50c Provider paused — sustained outage",
                reply_hint="No reply needed — it resumes by itself once the provider works.",
                needs_reply=False,
            )
        for provider in sorted(previous - set(tripped)):
            log.info("provider_health.recovered", provider=provider.value)
            # Close the notice raised when it tripped. Without this the breaker
            # self-heals in seconds while its escalation sits open indefinitely,
            # telling an operator a provider is down that recovered hours ago --
            # confidently wrong, which is worse than silent. Matched on the same
            # subject the trip used, so it can only ever close its own notice.
            closed = await repository.resolve_open_interrupts_by_subject(
                session,
                reason=InterruptReason.PROVIDER_OUTAGE,
                subject=f"{provider.value} is in a sustained outage",
                response=(
                    f"Resolved automatically: {provider.value} is answering again. "
                    "Work that was deferred while it was down resumes on its own."
                ),
            )
            if closed:
                log.info("provider_health.notice_closed", provider=provider.value, closed=closed)

    async def _schedule_discovery(self, session: Any) -> None:
        """Enqueue a discovery cycle on cadence, unless Development is saturated.

        Throttling here as well as in the Orchestrator is intentional: the
        Orchestrator stops a full pipeline being *fed*, this stops the queue being
        *filled* in the first place.
        """
        ctx = self._ctx
        if not ctx.is_enabled(EngineName.DISCOVERY):
            return

        active = await repository.count_active_dev_tasks(session)
        if active >= ctx.settings.max_concurrent_builds:
            log.debug("discovery.throttled", active=active)
            return

        interval = ctx.settings.discovery_interval_seconds
        bucket = int(datetime.now(UTC).timestamp() // interval)
        created = await repository.enqueue_task(
            session,
            kind=TaskKind.DISCOVER_OPPORTUNITIES,
            engine=EngineName.DISCOVERY,
            idempotency_key=f"discovery:cycle:{bucket}",
            payload={"window_start": bucket * interval},
            priority=200,
        )
        if created:
            log.info("discovery.scheduled", task_id=created, window=bucket)

    async def _schedule_periodic(self, session: Any) -> None:
        """Cadences that are not event-driven.

        Finance previously only ran when a Stripe webhook arrived, so a factory that
        had not yet made a sale — or whose webhook was misconfigured — never
        reconciled at all, and the virtual card limit silently drifted from the
        ledger. Marketing had the same shape: outreach only happened right after a
        deploy, so a live product stopped acquiring customers the moment its first
        batch was sent.
        """
        ctx = self._ctx
        now = datetime.now(UTC)

        if ctx.is_enabled(EngineName.FINANCE):
            await repository.enqueue_task(
                session,
                kind=TaskKind.RECONCILE_REVENUE,
                engine=EngineName.FINANCE,
                idempotency_key=f"reconcile:{_bucket(now, 3600)}",
                payload={},
                priority=10,  # money first
            )

        if ctx.is_enabled(EngineName.MARKETING):
            for product in await repository.live_products(session, limit=5):
                await repository.enqueue_task(
                    session,
                    kind=TaskKind.HARVEST_LEADS,
                    engine=EngineName.MARKETING,
                    idempotency_key=f"leads:{product.id}:{_bucket(now, 12 * 3600)}",
                    payload={"product_id": product.id},
                    product_id=product.id,
                    priority=120,
                )

            # The stalled-validation net: unlike the live-products sweep above,
            # HARVEST_LEADS for a *validating* product is a one-shot enqueue
            # (see development/graph.py) -- nothing else ever asks again on its
            # own. A validation stuck at zero leads (an external provider
            # credential rejected, or a niche too narrow for either provider)
            # previously just sat there forever with no automatic path forward
            # and nothing telling a human -- observed live. Checked hourly
            # (cheap: a few counts, not a paid lookup) so a fresh stall is
            # caught promptly, but the actual retry/escalate decision is gated
            # by real elapsed time (`validation_stalled_hours`), not this
            # cadence.
            for product in await repository.stalled_validations(
                session,
                older_than=timedelta(hours=ctx.settings.validation_stalled_hours),
                limit=5,
            ):
                if not await repository.has_retried_lead_sourcing(session, product.id):
                    # First strike: try once more, exactly like a person finding
                    # the internet down would first check whether it is back up
                    # before bothering anyone.
                    await repository.enqueue_task(
                        session,
                        kind=TaskKind.HARVEST_LEADS,
                        engine=EngineName.MARKETING,
                        idempotency_key=f"leads:validate:{product.id}:retry",
                        payload={"product_id": product.id},
                        product_id=product.id,
                        priority=120,
                    )
                elif not await repository.has_attempted_channel_scout(session, product.id):
                    # Second strike: the retry didn't help either, but that's not
                    # yet a reason to ask a human -- try a different distribution
                    # channel first. `_channel_scout_review` (marketing/graph.py)
                    # reads `last_lead_sourcing_summary` so the scout knows *why*
                    # the current channel came up empty, not just that it did.
                    await repository.enqueue_task(
                        session,
                        kind=TaskKind.CHANNEL_SCOUT_REVIEW,
                        engine=EngineName.MARKETING,
                        idempotency_key=f"channel-scout:stall:{product.id}",
                        payload={"product_id": product.id},
                        product_id=product.id,
                        priority=100,
                    )
                else:
                    # Third strike: a retry and an automatic channel-scout review
                    # both failed to unstick this -- this is not something more
                    # waiting or another automatic channel can fix on its own.
                    # A fresh id, not product.id[:8]: stalled_validations() already
                    # excludes any product with an open interrupt, so this raise is
                    # never a duplicate of a still-open one -- but product.id is
                    # stable across this product's whole life, and would collide
                    # with an earlier, now-resolved escalation for the same product
                    # (see 0017_interrupt_open_subject_partial.py).
                    await interrupts.request(
                        session,
                        reason=InterruptReason.LEAD_SOURCING_STALLED,
                        subject=f"{product.name[:80]} can't source leads ({new_id()[:8]})",
                        detail=(
                            "Two lead-sourcing attempts and an automatic channel-"
                            "scout review all found nothing for this validating "
                            "product. This usually means a lead-provider "
                            "credential (Hunter/Apollo) is present but being "
                            "rejected, not that it's genuinely missing -- worth "
                            "checking the provider account directly."
                        ),
                        headline="\U0001f511 Account problem — can't source leads",
                        reply_hint=(
                            "Reply anything to retry sourcing leads, or REJECT to "
                            "retire this product."
                        ),
                        product_id=product.id,
                    )

            # CHANNEL_SCOUT's reliability net -- the Orchestrator can spawn it
            # directly (it is in role_catalogue()); this is what still finds a
            # quiet product even if that judgement call never happens. A longer
            # cadence than HARVEST_LEADS since channel discovery is a heavier,
            # rarer activity than a routine lead sweep.
            scout_bucket = _bucket(now, ctx.settings.channel_scout_interval_days * 86400)
            for product in await repository.products_needing_channel_scout(
                session,
                older_than=timedelta(days=ctx.settings.channel_scout_interval_days),
                limit=3,
            ):
                await repository.enqueue_task(
                    session,
                    kind=TaskKind.CHANNEL_SCOUT_REVIEW,
                    engine=EngineName.MARKETING,
                    idempotency_key=f"channel-scout:{product.id}:{scout_bucket}",
                    payload={"product_id": product.id},
                    product_id=product.id,
                    priority=70,
                )

            # A pure DB decision (see marketing/graph.py::_measure_channel_experiment)
            # -- checked hourly, matching the AB-test sweep below, since it costs
            # nothing but a count, not an LLM call.
            for experiment in await repository.approved_channel_experiments_awaiting_conclusion(
                session, older_than=timedelta(days=ctx.settings.channel_experiment_measure_days)
            ):
                await repository.enqueue_task(
                    session,
                    kind=TaskKind.MEASURE_CHANNEL_EXPERIMENT,
                    engine=EngineName.MARKETING,
                    idempotency_key=f"measure-channel:{experiment.id}:{_bucket(now, 3600)}",
                    payload={"experiment_id": experiment.id},
                    product_id=experiment.product_id,
                    priority=130,
                )

        if ctx.is_enabled(EngineName.DEVELOPMENT):
            # A pure DB decision (see development/graph.py::_evaluate_ab_test) --
            # checked far more often than HARVEST_LEADS above since it costs
            # nothing but a couple of counts, not an LLM call or a paid lookup.
            for ab_test in await repository.running_ab_tests(session, limit=20):
                await repository.enqueue_task(
                    session,
                    kind=TaskKind.EVALUATE_AB_TEST,
                    engine=EngineName.DEVELOPMENT,
                    idempotency_key=f"ab_test:{ab_test.id}:{_bucket(now, 3600)}",
                    payload={"ab_test_id": ab_test.id},
                    product_id=ab_test.product_id,
                    priority=110,
                )

            # GROWTH_ANALYST's reliability net -- same reasoning as the
            # CHANNEL_SCOUT sweep above: the Orchestrator can spawn it directly,
            # this is the backstop so a quiet product still gets looked at.
            review_bucket = _bucket(now, ctx.settings.growth_review_interval_days * 86400)
            for product in await repository.products_needing_growth_review(
                session,
                older_than=timedelta(days=ctx.settings.growth_review_interval_days),
                limit=3,
            ):
                await repository.enqueue_task(
                    session,
                    kind=TaskKind.GROWTH_REVIEW,
                    engine=EngineName.DEVELOPMENT,
                    idempotency_key=f"growth-review:{product.id}:{review_bucket}",
                    payload={"product_id": product.id},
                    product_id=product.id,
                    priority=65,
                )

            # Standing product oversight: AUDITOR walks each live product with a
            # real browser (development/graph.py::_review_live_product).
            #
            # Deliberately here rather than in `backlog.py::CATALOGUE` alongside the
            # deterministic `audit-live-products` slug. That catalogue is only ever
            # enqueued from `graph.py::_backlog`, reached solely on IDLE_BACKLOG /
            # HOLD_DISCOVERY -- so a *busy* factory never reviews its live products
            # at all, which is exactly when a shipped defect is most likely to go
            # unnoticed. This sweep runs unconditionally, like the ones above.
            live_review_bucket = _bucket(now, ctx.settings.live_review_interval_days * 86400)
            for product in await repository.live_products(session, limit=5):
                await repository.enqueue_task(
                    session,
                    kind=TaskKind.REVIEW_LIVE_PRODUCT,
                    engine=EngineName.DEVELOPMENT,
                    idempotency_key=f"live-review:{product.id}:{live_review_bucket}",
                    payload={"product_id": product.id},
                    product_id=product.id,
                    priority=64,
                )

            # A pure API check (see development/graph.py::_verify_domain) -- no
            # LLM call, so it costs nothing to recheck hourly, same cadence as
            # the AB-test sweep above. DNS propagation can take anywhere from
            # minutes to days, so this just keeps trying until Vercel confirms.
            for product in await repository.products_with_attached_domain(session, limit=20):
                await repository.enqueue_task(
                    session,
                    kind=TaskKind.VERIFY_DOMAIN,
                    engine=EngineName.DEVELOPMENT,
                    idempotency_key=f"verify-domain:{product.id}:{_bucket(now, 3600)}",
                    payload={"product_id": product.id},
                    product_id=product.id,
                    priority=115,
                )

    async def tick(self) -> dict[str, Any]:
        self._tick += 1
        await self.housekeeping()
        config = {"configurable": {"thread_id": f"tick:{self._tick}"}}
        result = await self._graph.ainvoke({"tick": self._tick}, config=config)
        return dict(result)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        interval = self._ctx.settings.supervisor_tick_seconds
        log.info("supervisor.started", worker=self._ctx.worker_id, tick_seconds=interval)
        while not stop.is_set():
            started = datetime.now(UTC)
            try:
                await self.tick()
            except Exception:
                log.exception("supervisor.tick_failed", tick=self._tick)
            elapsed = (datetime.now(UTC) - started).total_seconds()
            remaining = max(0.0, interval - elapsed)
            try:
                await asyncio.wait_for(stop.wait(), timeout=remaining)
            except TimeoutError:
                continue
        log.info("supervisor.stopped", ticks=self._tick)

    async def release_all_leases(self) -> None:
        """On graceful shutdown, hand every claimed task straight back."""
        async with session_scope(self._ctx.settings) as session:
            from sqlalchemy import CursorResult, update

            from factory.state.orm import Task
            from factory.state.schemas import TaskStatus

            result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
                update(Task)
                .where(Task.claimed_by == self._ctx.worker_id, Task.status != TaskStatus.DONE.value)
                .values(
                    status=TaskStatus.PENDING.value,
                    claimed_by=None,
                    lease_expires_at=None,
                    not_before=datetime.now(UTC) + timedelta(seconds=1),
                )
            )
            if result.rowcount:
                log.info("supervisor.leases_released", count=result.rowcount)
