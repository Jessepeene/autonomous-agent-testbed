"""All database access.

Engines never write SQL. They call these functions, which own the invariants:
idempotent enqueue, leased claims that survive a crash, the never-negative ledger,
and the minimisation boundary for leads.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, case, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from factory import issues
from factory.engines.compliance.minimize import minimize
from factory.logging import get_logger
from factory.settings import EngineName, Settings, get_settings
from factory.state.orm import (
    ABTest,
    CampaignApproval,
    ChannelExperiment,
    CustomCredential,
    Customer,
    DunningEvent,
    EntityVerdictRow,
    HealAttempt,
    Idea,
    IdeaEvidence,
    IntegrationAlert,
    Interrupt,
    Issue,
    Lead,
    LedgerEntry,
    LLMCall,
    MarketingFreeze,
    OutreachLog,
    PageView,
    Product,
    ProductLock,
    PypiPackageCheck,
    Question,
    RateLimitBucket,
    Requirement,
    ScoutSignal,
    ScrapeDomainFailure,
    Subscription,
    Task,
    TaskEvent,
    ValidationSignal,
    new_id,
)
from factory.state.schemas import (
    InterruptReason,
    LeadRecord,
    ProductStatus,
    ScoredIdea,
    TaskKind,
    TaskStatus,
    ValidatedIdea,
    WorkRequirements,
)

log = get_logger(__name__)

ACTIVE_DEV_STATUSES = (
    ProductStatus.BUILDING.value,
    ProductStatus.REVIEWING.value,
    ProductStatus.PATCHING.value,
)


def _now() -> datetime:
    return datetime.now(UTC)


def fingerprint(*parts: str) -> str:
    joined = "␟".join(p.strip().lower() for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:64]


# ------------------------------------------------------------------------- tasks


async def enqueue_task(
    session: AsyncSession,
    *,
    kind: TaskKind,
    engine: EngineName,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    idea_id: str | None = None,
    product_id: str | None = None,
    not_before: datetime | None = None,
) -> str | None:
    """Insert a task unless the same logical work is already queued.

    Returns the new task id, or ``None`` when the idempotency key already existed —
    which makes "enqueue then crash then enqueue again" safe.
    """
    stmt = (
        pg_insert(Task)
        .values(
            kind=kind.value,
            engine=engine.value,
            status=TaskStatus.PENDING.value,
            priority=priority,
            payload=payload or {},
            idea_id=idea_id,
            product_id=product_id,
            idempotency_key=idempotency_key,
            not_before=not_before,
        )
        .on_conflict_do_nothing(index_elements=[Task.idempotency_key])
        .returning(Task.id)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    return str(row) if row is not None else None


async def claim_next_task(
    session: AsyncSession,
    *,
    worker_id: str,
    engines: Sequence[EngineName],
    settings: Settings | None = None,
) -> Task | None:
    """Lease the highest-priority ready task for one of the given engines.

    ``FOR UPDATE SKIP LOCKED`` means several workers can claim concurrently without
    ever handing the same task to two of them.
    """
    settings = settings or get_settings()
    now = _now()
    stmt = (
        select(Task)
        .where(
            Task.status == TaskStatus.PENDING.value,
            Task.engine.in_([e.value for e in engines]),
            (Task.not_before.is_(None)) | (Task.not_before <= now),
        )
        .order_by(Task.priority.asc(), Task.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = (await session.execute(stmt)).scalar_one_or_none()
    if task is None:
        return None

    task.status = TaskStatus.CLAIMED.value
    task.claimed_by = worker_id
    task.attempt_count += 1
    task.lease_expires_at = now + timedelta(seconds=settings.task_lease_seconds)
    await session.flush()
    return task


async def reclaim_expired_leases(session: AsyncSession) -> int:
    """Return crashed workers' tasks to the queue. Called on every tick and at boot."""
    stmt = (
        update(Task)
        .where(
            Task.status.in_([TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value]),
            Task.lease_expires_at.isnot(None),
            Task.lease_expires_at < _now(),
        )
        .values(status=TaskStatus.PENDING.value, claimed_by=None, lease_expires_at=None)
    )
    result = cast("CursorResult[Any]", await session.execute(stmt))
    return int(result.rowcount or 0)


async def complete_task(session: AsyncSession, task_id: str) -> None:
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.DONE.value,
            claimed_by=None,
            lease_expires_at=None,
            last_error=None,
        )
    )


async def fail_task(
    session: AsyncSession,
    task_id: str,
    error: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """Record a failure. Returns True when the task has exhausted its attempts.

    Three autonomous attempts, then escalate — the decision hierarchy's last step.
    """
    settings = settings or get_settings()
    task = await session.get(Task, task_id)
    if task is None:
        return False
    exhausted = task.attempt_count >= settings.max_task_attempts
    task.status = TaskStatus.ESCALATED.value if exhausted else TaskStatus.PENDING.value
    task.claimed_by = None
    task.lease_expires_at = None
    task.last_error = error[:4000]
    if not exhausted:
        backoff = min(2**task.attempt_count, 60)
        task.not_before = _now() + timedelta(seconds=backoff)
    await session.flush()
    return exhausted


async def reopen_escalated_task(session: AsyncSession, task_id: str) -> bool:
    """Give an escalated task a fresh attempt budget.

    The human-facing counterpart to `fail_task`'s exhaustion path: a reply
    confirming the root cause is fixed (see `hitl.interrupts.collect_replies`)
    turns back into exactly the state a task is in right after `enqueue_task` --
    nothing here is retry-specific, it is just undoing exhaustion. Only acts on a
    task that is actually `escalated`; replying to an interrupt whose task already
    moved on (retried some other way, or the product was retired) is a no-op, not
    an error.
    """
    task = await session.get(Task, task_id)
    if task is None or task.status != TaskStatus.ESCALATED.value:
        return False
    task.status = TaskStatus.PENDING.value
    task.attempt_count = 0
    task.claimed_by = None
    task.lease_expires_at = None
    task.not_before = None
    task.last_error = None
    await session.flush()
    return True


#: Retention is tied to the owning product's lifecycle, not a fixed calendar cutoff
#: -- see `purge_product_pii`. Left short and unambiguous so a human auditing the
#: table can tell a purge from a normal empty/short escalation reply at a glance.
_PII_REDACTED = "[redacted: retention tied to product lifecycle, see purge_product_pii]"


async def retire_product(session: AsyncSession, product_id: str) -> bool:
    """The single terminal transition for a product, however it was reached (a
    human's explicit REJECT to a heal-exhaustion escalation, or a validation gate
    that never cleared its interest bar). Terminal -- nothing else in this codebase
    ever moves a product out of ``retired``.

    Also the one place PII retention is enforced: once a product can never do
    anything else, the free-text fields that carried professional contact context
    about real people (`CampaignApproval.brief`, `Interrupt.detail`) have no further
    operational purpose.
    """
    product = await session.get(Product, product_id)
    if product is None:
        return False
    product.status = ProductStatus.RETIRED.value
    await purge_product_pii(session, product_id)
    await session.flush()
    return True


async def purge_product_pii(session: AsyncSession, product_id: str) -> int:
    """Redact the free-text fields a retired product no longer needs.

    Subject/status/timestamps are left intact everywhere -- the audit trail (what
    kind of thing happened, when, how it was resolved) survives; only the free text
    a human or model wrote role/company detail into does not. Idempotent: already-
    redacted rows are excluded from the ``WHERE``, so calling this twice (e.g. a
    retry after a crash) does not inflate the returned count.
    """
    interrupt_ids = (
        (
            await session.execute(
                select(Interrupt.id).where(
                    or_(
                        Interrupt.product_id == product_id,
                        Interrupt.task_id.in_(select(Task.id).where(Task.product_id == product_id)),
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    purged = 0
    if interrupt_ids:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(Interrupt)
                .where(Interrupt.id.in_(interrupt_ids), Interrupt.detail != _PII_REDACTED)
                .values(detail=_PII_REDACTED)
            ),
        )
        purged += result.rowcount or 0

    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(CampaignApproval)
            .where(
                CampaignApproval.product_id == product_id,
                CampaignApproval.brief != _PII_REDACTED,
            )
            .values(brief=_PII_REDACTED)
        ),
    )
    purged += result.rowcount or 0
    return purged


async def enqueue_guided_patch(
    session: AsyncSession, *, product_id: str, guidance: str, interrupt_id: str
) -> str | None:
    """A human's explicit guidance reply to a heal-exhaustion escalation -- exactly
    one more patch attempt, not a reset of the heal budget.

    ``product.heal_attempts`` is left untouched (already at ``MAX_HEAL_CYCLES``): if
    this attempt also fails, ``_heal()`` computes ``attempts > MAX_HEAL_CYCLES`` and
    re-escalates immediately instead of silently burning further autonomous cycles --
    a human has to explicitly ask for each additional try, which matters because each
    one is a real code-generation call, not a free retry. ``interrupt_id`` (unique per
    escalation) makes the idempotency key unique per guided attempt -- a product can
    re-escalate and be guided again more than once, and a fixed key would silently
    swallow every attempt after the first via ON CONFLICT DO NOTHING.
    """
    product = await session.get(Product, product_id)
    if product is None or product.idea_id is None:
        return None
    return await enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"patch:{product_id}:human-guided:{interrupt_id}",
        payload={
            "product_id": product_id,
            "idea_id": product.idea_id,
            # Provenance leads the brief, matching `queue_review_patch`: the repair
            # agent's framing asks where a symptom was seen, and "the operator said
            # so" is a different kind of evidence from "a QA pass reproduced it on
            # the deployment" -- one is an instruction, the other an observation.
            "healing": f"From the human operator, in reply to an escalation.\n\n{guidance}"[:6000],
            # Routes to the surgical repair path (see `_should_repair_surgically`).
            # A human who has diagnosed the problem and said what to change is the
            # single best case for editing in place: regenerating an entire live
            # product from scratch over one instruction is both the most expensive
            # option and the one most likely to break things that currently work.
            "category": "guided",
        },
        product_id=product_id,
        priority=40,
    )


async def defer_task(
    session: AsyncSession, task_id: str, *, not_before: datetime, reason: str = "unspecified"
) -> None:
    """Return a task to the queue without counting it as a failed attempt.

    Distinct from `fail_task`: a task blocked on an external event it cannot control
    (a pending human approval) has not failed and must never move toward
    `max_task_attempts` exhaustion — it should simply be tried again later, for as
    long as it takes.

    ``reason`` is recorded to the causal trail. Before it existed, a deferral stored
    only a future timestamp, so a campaign-approval wait, a marketing freeze, a
    validation-capacity hold, a blocked repair and a provider outage were
    **indistinguishable in the database** -- the difference survived only in a log
    line that the next deploy destroyed. A queue that stops moving could not be told
    apart from one that was waiting on a human.
    """
    await session.execute(
        update(Task)
        .where(Task.id == task_id)
        .values(
            status=TaskStatus.PENDING.value,
            claimed_by=None,
            lease_expires_at=None,
            not_before=not_before,
        )
    )
    task = await session.get(Task, task_id)
    await record_task_event(
        session,
        event="task.deferred",
        task_id=task_id,
        product_id=task.product_id if task else None,
        engine=EngineName(task.engine) if task and task.engine else None,
        detail={"reason": reason, "not_before": not_before.isoformat()},
    )


async def count_active_dev_tasks(session: AsyncSession) -> int:
    """Products currently building, under review, or being patched.

    Drives the Discovery throttle: at capacity, new ideas wait rather than piling up
    faster than Development can drain them.
    """
    stmt = select(func.count()).select_from(Product).where(Product.status.in_(ACTIVE_DEV_STATUSES))
    return int((await session.execute(stmt)).scalar_one())


async def count_pending_dev_retries(session: AsyncSession) -> int:
    """Pending Development tasks (``BUILD_MVP`` or ``PATCH_PRODUCT``) whose product
    is *already* counted in `count_active_dev_tasks`.

    Generalises what used to be a `PATCH_PRODUCT`-only check: a retried `BUILD_MVP`
    for a product stuck in `BUILDING` after an earlier attempt threw (the status is
    set before the attempt and never reset on failure) is exactly the same shape of
    problem -- claiming it doesn't admit a new product into the pipeline either, so
    the capacity gate must not hold it back the way it holds back a genuinely new
    build. Without this, two failed builds in a row can permanently fill both of
    `max_concurrent_builds`' slots with products whose only pending work is a retry
    the gate never recognises as safe to claim.
    """
    stmt = (
        select(func.count())
        .select_from(Task)
        .join(Product, Product.id == Task.product_id)
        .where(
            Task.status == TaskStatus.PENDING.value,
            Task.engine == EngineName.DEVELOPMENT.value,
            Task.kind.in_((TaskKind.BUILD_MVP.value, TaskKind.PATCH_PRODUCT.value)),
            Product.status.in_(ACTIVE_DEV_STATUSES),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_active_validations(session: AsyncSession) -> int:
    """Products currently running a validation (landing page + outreach).

    Capped separately from `count_active_dev_tasks`/`max_concurrent_builds`: a
    validation is deliberately much cheaper than a real MVP build and must not
    compete with real builds for the same capacity slots, but it still needs its
    own cap -- otherwise every approved idea spawns an unbounded parallel
    validation, flooding the campaign-approval queue and burning the shared,
    metered lead provider quota before any of them prove out.
    """
    stmt = (
        select(func.count())
        .select_from(Product)
        .where(Product.status == ProductStatus.VALIDATING.value)
    )
    return int((await session.execute(stmt)).scalar_one())


async def count_pending_tasks(
    session: AsyncSession,
    engine: EngineName | None = None,
    *,
    kind: TaskKind | None = None,
) -> int:
    """Tasks that could actually be claimed right now.

    The `not_before` filter matches `claim_next_task`'s exactly, and that
    agreement is load-bearing. Without it a *deferred* task -- one waiting on a
    campaign approval, a tripped provider, or validation capacity -- still counts
    as pending, so the Orchestrator's snapshot reports work that by construction
    cannot move. It then sees a queue that never drains, concludes the factory is
    deadlocked, and escalates.

    Observed live: 45 "Factory is stuck" escalations in three hours, every one of
    them counting 16 discovery tasks that had been deliberately deferred and could
    not run for another 30 days. The provider circuit breaker makes this sharper
    still -- pausing a provider defers its tasks, which under the old count looked
    exactly like a stall and would have had the breaker trigger the very alarm
    storm it exists to prevent.
    """
    now = _now()
    stmt = (
        select(func.count())
        .select_from(Task)
        .where(
            Task.status == TaskStatus.PENDING.value,
            (Task.not_before.is_(None)) | (Task.not_before <= now),
        )
    )
    if engine is not None:
        stmt = stmt.where(Task.engine == engine.value)
    if kind is not None:
        stmt = stmt.where(Task.kind == kind.value)
    return int((await session.execute(stmt)).scalar_one())


async def recent_tasks(session: AsyncSession, *, limit: int = 50) -> list[Task]:
    """Newest-first, unfiltered by status -- the dashboard's task-queue view.
    No row-level task listing existed anywhere before this; every other task
    query in this module is a `count()` or a status-specific work-queue claim."""
    stmt = select(Task).order_by(Task.updated_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def tasks_in_flight_by_engine(session: AsyncSession) -> dict[str, int]:
    """Count of genuinely active (not stale-claimed) tasks per engine -- the
    dashboard's "is this engine currently running something" signal.

    The `lease_expires_at` filter matters: a worker that crashed mid-task
    leaves its claim behind until `reclaim_expired_leases` next sweeps it, and
    without this filter that abandoned claim would read as "running" for
    however long it sits unswept, the same staleness `reclaim_expired_leases`
    itself exists to fix -- this is the read-side equivalent.
    """
    stmt = (
        select(Task.engine, func.count())
        .where(
            Task.status.in_((TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value)),
            or_(Task.lease_expires_at.is_(None), Task.lease_expires_at > _now()),
        )
        .group_by(Task.engine)
    )
    return {str(engine): int(count) for engine, count in (await session.execute(stmt)).all()}


# ------------------------------------------------------------------------- ideas


async def used_idea_source_urls(session: AsyncSession) -> set[str]:
    """Every `source_url` any idea has ever been generated from, trailing slash
    stripped -- matches `discovery/graph.py::_evidence_count_for`'s own
    normalisation, the only other place this codebase already compares a
    signal's URL against a stored idea's.

    `upsert_idea`'s fingerprint dedup only catches a byte-for-byte repeat of
    the *generated prose*, which near-never recurs -- an LLM naturally
    rephrases the same evidence into different wording on every call. Real
    bug, found live: the same real GitHub issue won the top-ranked cluster
    slot 5 cycles running (nothing about its comment count changes hour to
    hour), and the generator produced 5 differently-worded ideas from the
    identical evidence, all 5 passing the fingerprint check and becoming 5
    separate products. `source_url` is the one fact the generator is
    required to copy verbatim rather than paraphrase, making it the actual
    reliable signal for "have we already mined this."
    """
    rows = (await session.execute(select(Idea.source_url))).scalars().all()
    return {url.rstrip("/") for url in rows}


async def upsert_idea(
    session: AsyncSession,
    idea: ScoredIdea,
    *,
    score: float | None = None,
    evidence_count: int = 0,
) -> str | None:
    """Store a scored idea, de-duplicated by problem + niche fingerprint.

    ``score`` defaults to ``idea.score`` (the fixed historical weights) — pass it
    explicitly only when the caller has fetched tuned weights and wants
    ``idea.weighted_score(weights)`` stored instead.

    ``evidence_count`` defaults to 0 for any caller that predates it (tests
    constructing an idea directly, for instance) -- ``discovery/graph.py`` is the
    only real caller, and it passes the source cluster's signal count.
    """
    stored_score = score if score is not None else idea.score
    key = fingerprint(idea.problem_statement, idea.target_niche)
    stmt = (
        pg_insert(Idea)
        .values(
            problem_statement=idea.problem_statement,
            target_niche=idea.target_niche,
            source_url=str(idea.source_url),
            monetization_hypothesis=idea.monetization_hypothesis,
            score=stored_score,
            evidence_count=evidence_count,
            fingerprint=key,
            status="pending",
            # Per-dimension breakdown behind `score` -- see the columns' own
            # comments in state/orm.py for why this enables backtesting future
            # weight changes. `.value` on the two enum fields, never the member
            # itself: the column is a plain string, matching every other status
            # column in this schema.
            pain_severity=idea.pain_severity,
            willingness_to_pay=idea.willingness_to_pay,
            build_feasibility=idea.build_feasibility,
            adoption_ease=idea.adoption_ease,
            defensibility=idea.defensibility,
            platform_dependency=idea.platform_dependency.value,
            replacement_mode=idea.replacement_mode.value,
            self_serve_price_usd=idea.self_serve_price_usd,
        )
        .on_conflict_do_nothing(index_elements=[Idea.fingerprint])
        .returning(Idea.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def next_validated_idea(session: AsyncSession) -> ValidatedIdea | None:
    stmt = (
        select(Idea)
        .where(Idea.status == "approved")
        .order_by(Idea.score.desc(), Idea.created_at.asc())
        .limit(1)
    )
    idea = (await session.execute(stmt)).scalar_one_or_none()
    if idea is None:
        return None
    return ValidatedIdea(
        id=idea.id,
        problem_statement=idea.problem_statement,
        target_niche=idea.target_niche,
        source_url=idea.source_url,
        monetization_hypothesis=idea.monetization_hypothesis,
        score=idea.score,
        evidence_count=idea.evidence_count,
        self_serve_price_usd=idea.self_serve_price_usd,
        created_at=idea.created_at,
    )


async def set_idea_status(session: AsyncSession, idea_id: str, status: str) -> None:
    await session.execute(update(Idea).where(Idea.id == idea_id).values(status=status))


async def get_idea(session: AsyncSession, idea_id: str) -> Idea | None:
    """A plain row fetch, routed through `repository` (rather than a bare
    `session.get(Idea, idea_id)` at the call site) so callers stay mockable the
    same way every other DB access in this module already is -- see
    `development/graph.py::_verify_and_ship`'s price lookup, exercised in tests
    that pass no real session at all."""
    return await session.get(Idea, idea_id)


async def prior_verdicts_for_niche(
    session: AsyncSession, niche: str, *, limit: int = 5
) -> list[tuple[str, str]]:
    """``(target_niche, status)`` pairs for past ideas lexically similar to
    ``niche`` -- structured facts for the compliance assessor, deliberately
    NOT a memory recall. Injecting model-authored memory text into
    `IDEA_SYSTEM_PROMPT` would be a de-facto mutation channel into a
    safety-critical gate, exactly what `learning/allowlist.py` permanently
    excludes compliance prompts to prevent; a fact read from this table
    cannot be poisoned by a prior model's prose the way a memory could.

    An exact niche match is nearly impossible in practice --
    `upsert_idea`'s fingerprint dedup already prevents a literal repeat from
    ever becoming a second row -- so this scores every non-pending idea's
    niche against the query with the same token-overlap function
    `memory/store.py` and `discovery/cluster.py` already use, rather than
    inventing a second similarity metric.
    """
    from factory.engines.discovery.cluster import jaccard, tokenize

    query_tokens = tokenize(niche)
    stmt = (
        select(Idea.target_niche, Idea.status)
        .where(Idea.status != "pending")
        .order_by(Idea.created_at.desc())
        .limit(500)
    )
    rows = (await session.execute(stmt)).all()
    scored = sorted(
        (
            (jaccard(query_tokens, tokenize(row.target_niche)), row.target_niche, row.status)
            for row in rows
        ),
        key=lambda entry: entry[0],
        reverse=True,
    )
    return [(niche_text, status) for relevance, niche_text, status in scored if relevance >= 0.2][
        :limit
    ]


async def pending_ideas(session: AsyncSession, limit: int = 20) -> list[Idea]:
    stmt = (
        select(Idea)
        .where(Idea.status == "pending")
        .order_by(Idea.score.desc(), Idea.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------- products


async def create_product(session: AsyncSession, *, name: str, idea_id: str) -> str:
    product = Product(name=name, idea_id=idea_id, status=ProductStatus.PLANNED.value)
    session.add(product)
    await session.flush()
    return product.id


async def set_product_status(
    session: AsyncSession,
    product_id: str,
    status: ProductStatus,
    **fields: Any,
) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(status=status.value, **fields)
    )


async def get_product(session: AsyncSession, product_id: str) -> Product | None:
    return await session.get(Product, product_id)


async def set_last_lead_sourcing_summary(
    session: AsyncSession, product_id: str, summary: str
) -> None:
    """`IngestReport.summary()`'s own text, persisted so it survives past the
    single `_harvest()` call that produced it -- see `Product.
    last_lead_sourcing_summary`'s own comment for why the report object
    itself can't be read back later."""
    await session.execute(
        update(Product)
        .where(Product.id == product_id)
        .values(last_lead_sourcing_summary=summary[:500])
    )


async def get_niche_for_product(session: AsyncSession, product_id: str) -> str | None:
    """`Product.idea_id -> Idea.target_niche` -- the one piece of context
    `finance/churn.py::write_churn_lessons` needs to file a memory at
    Discovery's own scope: a niche name, not a product id, which `recall`'s
    lexical matching could never use."""
    stmt = (
        select(Idea.target_niche)
        .join(Product, Product.idea_id == Idea.id)
        .where(Product.id == product_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_product_by_workspace(session: AsyncSession, workspace: str) -> Product | None:
    """The reverse lookup of `Product.workspace_path` -- workspace directory names
    are a truncated, one-way derivation of the product id (`f"product-{id[:12]}"`),
    so a caller holding only the workspace name (e.g. a sandbox tool invocation)
    cannot recover the owning product any other way."""
    stmt = select(Product).where(Product.workspace_path == workspace)
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_task_event(
    session: AsyncSession,
    *,
    event: str,
    task_id: str | None = None,
    product_id: str | None = None,
    engine: EngineName | None = None,
    detail: dict[str, Any] | None = None,
    caused_by_task_id: str | None = None,
) -> None:
    """Append one line to the causal trail. Never raises.

    Called from the hot path of every engine tick, so a failure to record history
    must never take down the work being recorded -- an observability write that can
    break the thing it observes is worse than no observability. Swallowed and
    logged, deliberately.
    """
    try:
        session.add(
            TaskEvent(
                task_id=task_id,
                product_id=product_id,
                engine=engine.value if engine else None,
                event=event[:64],
                detail=detail or {},
                caused_by_task_id=caused_by_task_id,
            )
        )
        await session.flush()
    except Exception as exc:  # pragma: no cover - defensive; see docstring
        # `event_name=`, not `event=`: structlog's first positional argument *is*
        # the event name, so passing one would collide with it.
        log.warning("task_event.write_failed", event_name=event, detail=str(exc)[:200])


async def product_history(
    session: AsyncSession, product_id: str, *, limit: int = 100
) -> list[TaskEvent]:
    """One product's whole story in order -- the query that did not exist before.

    Answering "what happened to this product" previously meant joining tasks,
    heal_attempts, llm_calls and interrupts by hand and inferring the gaps.
    """
    stmt = (
        select(TaskEvent)
        .where(TaskEvent.product_id == product_id)
        .order_by(TaskEvent.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def record_heal_attempt(
    session: AsyncSession,
    *,
    product_id: str,
    task_id: str | None,
    attempt_number: int,
    healing_text: str,
    failed_categories: list[str],
    blocking_at_attempt: int | None = None,
) -> str:
    """``attempt_number`` is the caller's *cycle* counter, which is not unique over
    a product's lifetime -- and this table's `uq_heal_attempts_product_attempt`
    requires that it is.

    `_heal` derives it from `product.heal_attempts`, which
    `_verify_and_ship` resets to 0 on every successful ship (so MAX_HEAL_CYCLES
    counts attempts within *this* repair cycle, which is what it should count).
    A product that heals, ships, and later needs healing again therefore starts a
    second cycle at 1 and collides with the first cycle's row. Latent until a
    product actually did that: observed live as a `UniqueViolationError` that
    escaped `_patch`'s `product_lock` as a `PendingRollbackError`, taking down the
    whole task with it.

    Resolved here rather than at the call site because the cycle counter is
    correct for its own purpose -- only the stored row needs to be unique. The
    caller's number is honoured when it does not collide, so the common case still
    reads 1, 2, 3.
    """
    highest = (
        await session.execute(
            select(func.max(HealAttempt.attempt_number)).where(
                HealAttempt.product_id == product_id
            )
        )
    ).scalar()
    row = HealAttempt(
        product_id=product_id,
        task_id=task_id,
        attempt_number=max(attempt_number, (highest or 0) + 1),
        healing_text=healing_text[:6000],
        failed_categories=failed_categories,
        blocking_at_attempt=blocking_at_attempt,
    )
    session.add(row)
    await session.flush()
    return row.id


async def resolve_pending_heal_attempts(
    session: AsyncSession, product_id: str, outcome: str
) -> None:
    """Resolves any heal attempt still ``pending`` for this product — in practice at
    most one, since a new attempt is only ever created after the previous one is
    resolved."""
    await session.execute(
        update(HealAttempt)
        .where(HealAttempt.product_id == product_id, HealAttempt.outcome == "pending")
        .values(outcome=outcome, resolved_at=_now())
    )


async def heal_attempts_for(
    session: AsyncSession, product_id: str, *, limit: int = 20
) -> list[HealAttempt]:
    """What has already been tried on this product, oldest first.

    The table's own docstring notes nothing reads it to make a decision. That is
    still true of the *control flow*, but the handbook now reads it for the
    product dossier: an employee about to repair something should be able to see
    that the same fix was attempted twice and failed, which is exactly the loop
    `MAX_HEAL_CYCLES` currently ends by exhaustion rather than by anyone noticing.
    """
    stmt = (
        select(HealAttempt)
        .where(HealAttempt.product_id == product_id)
        .order_by(HealAttempt.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def live_products(session: AsyncSession, limit: int = 20) -> list[Product]:
    stmt = (
        select(Product)
        .where(Product.status == ProductStatus.LIVE.value)
        .order_by(Product.updated_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def active_products(session: AsyncSession, limit: int = 20) -> list[Product]:
    """Every product still doing something -- everything except RETIRED, not just
    LIVE -- for `/status`'s Telegram report (`hitl/commands.py`), which should show
    a product mid-build or mid-validation too, not only ones already shipped."""
    stmt = (
        select(Product)
        .where(Product.status != ProductStatus.RETIRED.value)
        .order_by(Product.updated_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def products_with_idea(
    session: AsyncSession, *, limit: int = 50
) -> list[tuple[Product, Idea]]:
    """Every product (including RETIRED -- unlike `active_products`, the
    dashboard's portfolio view is a historical record, not a work queue)
    joined against its idea, since niche and score live on `Idea`, not
    `Product`."""
    stmt = (
        select(Product, Idea)
        .join(Idea, Idea.id == Product.idea_id)
        .order_by(Product.updated_at.desc())
        .limit(limit)
    )
    return [(product, idea) for product, idea in (await session.execute(stmt)).all()]


async def get_product_with_idea(
    session: AsyncSession, product_id: str
) -> tuple[Product, Idea] | None:
    """Single-row version of `products_with_idea` -- the dashboard's product
    detail view needs one product's business plan (niche/problem/monetization
    all live on `Idea`, not `Product`), not the whole portfolio list."""
    stmt = (
        select(Product, Idea)
        .join(Idea, Idea.id == Product.idea_id)
        .where(Product.id == product_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    product, idea = row
    return product, idea


async def set_growth_reviewed(session: AsyncSession, product_id: str) -> None:
    await _set_product_timestamp(session, product_id, "last_growth_review_at")


async def set_channel_scouted(session: AsyncSession, product_id: str) -> None:
    await _set_product_timestamp(session, product_id, "last_channel_scout_at")


async def _set_product_timestamp(session: AsyncSession, product_id: str, column: str) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(**{column: _now()})
    )


async def products_needing_growth_review(
    session: AsyncSession, *, older_than: timedelta, limit: int = 10
) -> list[Product]:
    """LIVE products GROWTH_ANALYST has never reviewed, or not recently -- what the
    periodic backstop sweep and the `list_products_needing_growth_review` tool both
    read from.

    A never-reviewed product's staleness is measured from ``created_at``, not
    treated as infinitely overdue -- a product that went live five minutes ago has
    no traffic data yet, and reviewing it immediately would compete with
    `HARVEST_LEADS` for the same tick's capacity for zero value. Once eligible,
    ordered oldest-effective-review-first, same idea either way.
    """
    cutoff = _now() - older_than
    effective = func.coalesce(Product.last_growth_review_at, Product.created_at)
    stmt = (
        select(Product)
        .where(Product.status == ProductStatus.LIVE.value, effective < cutoff)
        .order_by(effective.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def products_needing_channel_scout(
    session: AsyncSession, *, older_than: timedelta, limit: int = 10
) -> list[Product]:
    """Same reasoning as `products_needing_growth_review`, for CHANNEL_SCOUT.

    Includes VALIDATING alongside LIVE: a validating product blocked on its one
    channel previously had no periodic net at all -- only a LIVE product's
    established customer base got the "try another channel" backstop, even
    though a validating product with no leads needs it more.
    """
    cutoff = _now() - older_than
    effective = func.coalesce(Product.last_channel_scout_at, Product.created_at)
    stmt = (
        select(Product)
        .where(
            Product.status.in_([ProductStatus.LIVE.value, ProductStatus.VALIDATING.value]),
            effective < cutoff,
        )
        .order_by(effective.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def stalled_validations(
    session: AsyncSession, *, older_than: timedelta, limit: int = 10
) -> list[Product]:
    """VALIDATING products old enough that their one-shot validation harvest
    (``leads:validate:{id}``, see `development/graph.py::_build_landing_page`)
    has had time to run, but that still have zero leads at all -- the exact
    shape of a silent dead end. `HARVEST_LEADS` is enqueued exactly once per
    validating product; nothing else ever asks again on its own, so a product
    stuck this way (an external lead-provider credential rejected, or a niche
    too narrow for either provider) previously just sat there forever with no
    automatic path forward and nothing telling a human -- observed live.

    Excludes any product that already has an open interrupt -- a human is
    already being asked, so this must not pile on a second automatic action
    (a retry, or another escalation) while that reply is still pending.
    """
    cutoff = _now() - older_than
    no_leads = ~select(Lead.id).where(Lead.product_id == Product.id).exists()
    no_open_interrupt = (
        ~select(Interrupt.id)
        .where(Interrupt.product_id == Product.id, Interrupt.status == "open")
        .exists()
    )
    stmt = (
        select(Product)
        .where(
            Product.status == ProductStatus.VALIDATING.value,
            Product.created_at < cutoff,
            no_leads,
            no_open_interrupt,
        )
        .order_by(Product.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def has_retried_lead_sourcing(session: AsyncSession, product_id: str) -> bool:
    """Whether more than the original one-shot validation harvest has ever run
    for this product -- the signal the stalled-validation sweep
    (`scheduler.py::_schedule_periodic`) uses to decide "try once more"
    (the first time) vs. "ask a human" (every time after), regardless of
    whether that later attempt came from the sweep's own retry or a human's
    `#tag <answer>` reply asking for one.
    """
    stmt = (
        select(func.count())
        .select_from(Task)
        .where(Task.product_id == product_id, Task.kind == TaskKind.HARVEST_LEADS.value)
    )
    return int((await session.execute(stmt)).scalar_one()) >= 2


async def has_attempted_channel_scout(session: AsyncSession, product_id: str) -> bool:
    """The stalled-validation sweep's third rung: try a different distribution
    channel before escalating to a human, but only once ever for this product --
    whether that CHANNEL_SCOUT_REVIEW task came from this same stall ladder or
    from `products_needing_channel_scout`'s own periodic sweep. A channel that
    was already tried for any reason does not need trying again just because the
    sweep also happens to be looking at a stall right now.
    """
    stmt = (
        select(func.count())
        .select_from(Task)
        .where(Task.product_id == product_id, Task.kind == TaskKind.CHANNEL_SCOUT_REVIEW.value)
    )
    return int((await session.execute(stmt)).scalar_one()) >= 1


async def set_vercel_project(session: AsyncSession, product_id: str, project: str) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(vercel_project=project)
    )


async def set_domain_requested(session: AsyncSession, product_id: str) -> None:
    """Called once, right after `development/domain.py::ensure_requested` raises
    the interrupt -- marks the product so a later redeploy never asks again."""
    await session.execute(
        update(Product).where(Product.id == product_id).values(domain_status="pending")
    )


async def set_product_domain(session: AsyncSession, product_id: str, hostname: str) -> None:
    """The human's reply, captured -- `domain_status` stays 'pending' until the
    `ATTACH_DOMAIN` task actually calls Vercel."""
    await session.execute(
        update(Product).where(Product.id == product_id).values(custom_domain=hostname)
    )


async def set_domain_status(
    session: AsyncSession, product_id: str, status: str, **fields: Any
) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(domain_status=status, **fields)
    )


async def products_with_attached_domain(session: AsyncSession, limit: int = 20) -> list[Product]:
    """Domains Vercel has but DNS has not yet confirmed -- what the periodic
    `VERIFY_DOMAIN` sweep (scheduler.py) reads from."""
    stmt = select(Product).where(Product.domain_status == "attached").limit(limit)
    return list((await session.execute(stmt)).scalars().all())


# ------------------------------------------------------------------ product locks


async def acquire_product_lock(
    session: AsyncSession,
    *,
    product_id: str,
    holder: EngineName,
    reason: str,
    ttl_seconds: int = 3600,
    blocks_paid_spend: bool = True,
) -> bool:
    stmt = (
        pg_insert(ProductLock)
        .values(
            product_id=product_id,
            holder=holder.value,
            reason=reason,
            blocks_paid_spend=blocks_paid_spend,
            expires_at=_now() + timedelta(seconds=ttl_seconds),
        )
        .on_conflict_do_nothing(index_elements=[ProductLock.product_id])
        .returning(ProductLock.product_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def release_product_lock(session: AsyncSession, product_id: str) -> None:
    await session.execute(delete(ProductLock).where(ProductLock.product_id == product_id))


async def is_paid_spend_paused(session: AsyncSession, product_id: str) -> bool:
    """True while a product is unfit to receive paid traffic.

    Two conditions, and both are needed. The lock covers work in flight *right now*.
    The status covers the gap the lock cannot: when a build fails, the lock is
    released as the engine unwinds, but the repair task is only queued — so for the
    minutes or hours until a worker picks it up, a product known to be broken would
    otherwise look spendable. Status is the durable fact; the lock is just the
    in-flight marker on top of it.
    """
    lock_stmt = select(ProductLock).where(
        ProductLock.product_id == product_id,
        ProductLock.blocks_paid_spend.is_(True),
        ProductLock.expires_at > _now(),
    )
    if (await session.execute(lock_stmt)).scalar_one_or_none() is not None:
        return True

    status_stmt = select(Product.status).where(Product.id == product_id)
    status = (await session.execute(status_stmt)).scalar_one_or_none()
    return status in ACTIVE_DEV_STATUSES


async def purge_expired_locks(session: AsyncSession) -> int:
    result = cast(
        "CursorResult[Any]",
        await session.execute(delete(ProductLock).where(ProductLock.expires_at <= _now())),
    )
    return int(result.rowcount or 0)


# -------------------------------------------------------------------------- leads


async def insert_lead(
    session: AsyncSession,
    raw: Mapping[str, Any],
    *,
    lia_passed: bool,
    lia_justification: str | None = None,
    product_id: str | None = None,
    provenance_kind: str = "operator_supplied",
    provider: str = "operator-csv",
) -> LeadRecord:
    """The only sanctioned write path into ``leads``.

    Extraneous scraped keys are dropped by ``minimize()`` before the INSERT, and a
    lead with no ``source_url`` raises rather than being stored.
    """
    record = minimize(raw)
    stmt = (
        pg_insert(Lead)
        .values(
            first_name=record.first_name,
            last_name=record.last_name,
            job_title=record.job_title,
            company=record.company,
            work_email=record.work_email,
            source_url=record.source_url,
            scraped_at=record.scraped_at,
            provenance_kind=provenance_kind,
            provider=provider,
            lia_passed=lia_passed,
            lia_justification=lia_justification,
            product_id=product_id,
        )
        .on_conflict_do_nothing(index_elements=[Lead.work_email])
    )
    await session.execute(stmt)
    return record


_COMPANY_SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|corp|corporation|co)\.?\b")


def _normalize_company_name(company: str) -> str:
    """The same real company can be scraped with different punctuation or a
    different legal suffix on different passes ("Design Electric, Inc." vs
    "Design Electric Inc.") -- comparing normalized text, not ``==``, avoids
    treating them as two different companies. Ported from
    `reference files/lead_scout.py::_normalize_company`, where this exact gap
    was found in production."""
    text = company.lower()
    text = re.sub(r"[.,]", "", text)
    text = _COMPANY_SUFFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def has_lead_for_company(
    session: AsyncSession, *, company: str, name: str | None = None
) -> bool:
    """Pre-enrichment dedup for the web-search lead provider: avoids spending a
    Hunter Finder call on a company+name already known, *before* an email is even
    found -- complementary to (not a replacement for) `insert_lead`'s
    work_email-based ``ON CONFLICT`` at storage time, which only catches
    duplicates once an email already exists.

    Fetches every lead's company/name and compares in Python, matching
    `reference files/lead_scout.py::_already_have`'s own reasoning: the
    normalization isn't expressible as a simple SQL case-fold, and the leads
    table is small at this factory's current scale. Revisit with a narrower SQL
    filter if that stops being true.
    """
    target_company = _normalize_company_name(company)
    stmt = select(Lead.company, Lead.first_name, Lead.last_name)
    rows = (await session.execute(stmt)).all()
    matching_names = [
        f"{first} {last}".strip().lower()
        for existing_company, first, last in rows
        if _normalize_company_name(existing_company) == target_company
    ]
    if not matching_names:
        return False
    if not name:
        return True
    return name.strip().lower() in matching_names


async def recently_processed_urls(session: AsyncSession, *, since: datetime) -> set[str]:
    """Source URLs already scraped since ``since`` -- lets the web-search lead
    provider skip re-scraping the same evergreen directory/team page on every
    harvest call. Ported from
    `reference files/lead_scout.py::_recently_processed_urls`; production proof
    there: two directory URLs alone were independently re-extracted 9 and 2
    separate times within ~36 hours, accounting for 56% of every lead ever found.
    """
    stmt = select(Lead.source_url).where(Lead.scraped_at >= since)
    return set((await session.execute(stmt)).scalars().all())


async def set_lead_qualification_brief(
    session: AsyncSession, product_id: str, brief_json: str
) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(lead_qualification_brief=brief_json)
    )


async def set_design_brief(session: AsyncSession, product_id: str, brief_json: str) -> None:
    await session.execute(
        update(Product).where(Product.id == product_id).values(design_brief=brief_json)
    )


async def record_scrape_failure(
    session: AsyncSession, *, domain: str, reason: str, via_proxy: bool = False
) -> int:
    """First failure for a domain inserts ``failure_count=1``; each later one
    increments it and refreshes the reason/timestamp. Global, not per-product
    -- see `ScrapeDomainFailure`'s docstring.

    ``via_proxy=True`` increments ``proxy_failure_count`` instead --
    a domain that fails even through the datacenter proxy is a different,
    worse signal than one that just needed a different IP, and must not be
    conflated with (or silently reset) the direct-attempt count.

    Returns the count *after* this failure — ``RETURNING`` rather than a
    second `SELECT`, so a caller deciding whether this exact failure just
    crossed its blocking threshold doesn't need an extra round trip.
    """
    if via_proxy:
        stmt = (
            pg_insert(ScrapeDomainFailure)
            .values(
                domain=domain,
                failure_count=0,
                proxy_failure_count=1,
                last_reason=reason,
                last_failed_at=_now(),
            )
            .on_conflict_do_update(
                index_elements=[ScrapeDomainFailure.domain],
                set_={
                    "proxy_failure_count": ScrapeDomainFailure.proxy_failure_count + 1,
                    "last_reason": reason,
                    "last_failed_at": _now(),
                },
            )
            .returning(ScrapeDomainFailure.proxy_failure_count)
        )
    else:
        stmt = (
            pg_insert(ScrapeDomainFailure)
            .values(domain=domain, failure_count=1, last_reason=reason, last_failed_at=_now())
            .on_conflict_do_update(
                index_elements=[ScrapeDomainFailure.domain],
                set_={
                    "failure_count": ScrapeDomainFailure.failure_count + 1,
                    "last_reason": reason,
                    "last_failed_at": _now(),
                },
            )
            .returning(ScrapeDomainFailure.failure_count)
        )
    return int((await session.execute(stmt)).scalar_one())


async def blocked_scrape_domains(session: AsyncSession, *, threshold: int) -> set[str]:
    """Domains whose failure count has crossed ``threshold`` -- skipped before
    a fetch is even attempted on a later harvest."""
    stmt = select(ScrapeDomainFailure.domain).where(ScrapeDomainFailure.failure_count >= threshold)
    return set((await session.execute(stmt)).scalars().all())


async def proxy_required_domains(session: AsyncSession) -> set[str]:
    """Domains already known to need the datacenter proxy -- bulk-fetched once
    per harvest, same idiom as `blocked_scrape_domains`, so a domain that
    already proved it needs the proxy skips a doomed direct attempt (up to a
    real 20s timeout) on every single URL fetched from it."""
    stmt = select(ScrapeDomainFailure.domain).where(ScrapeDomainFailure.requires_proxy.is_(True))
    return set((await session.execute(stmt)).scalars().all())


async def mark_requires_proxy(session: AsyncSession, domain: str) -> None:
    """The direct attempt failed but the proxy succeeded -- set once, never
    reset automatically (see `ScrapeDomainFailure.requires_proxy`'s own
    comment for why)."""
    stmt = (
        pg_insert(ScrapeDomainFailure)
        .values(
            domain=domain,
            failure_count=0,
            last_reason="",
            last_failed_at=_now(),
            requires_proxy=True,
        )
        .on_conflict_do_update(
            index_elements=[ScrapeDomainFailure.domain], set_={"requires_proxy": True}
        )
    )
    await session.execute(stmt)


async def get_pypi_package_check(session: AsyncSession, name: str) -> PypiPackageCheck | None:
    return await session.get(PypiPackageCheck, name)


async def upsert_pypi_package_check(
    session: AsyncSession, *, name: str, exists: bool, first_release_at: datetime | None
) -> None:
    """Record (or refresh) a cached PyPI existence/age verdict. ``ON CONFLICT``
    updates in place rather than inserting a second row -- ``checked_at``'s
    ``onupdate=func.now()`` then does the TTL bookkeeping for free."""
    stmt = (
        pg_insert(PypiPackageCheck)
        .values(name=name, exists=exists, first_release_at=first_release_at)
        .on_conflict_do_update(
            index_elements=[PypiPackageCheck.name],
            set_={"exists": exists, "first_release_at": first_release_at},
        )
    )
    await session.execute(stmt)


async def erase_lead(
    session: AsyncSession, email: str, *, settings: Settings | None = None
) -> None:
    """GDPR Art. 17 erasure.

    Deleting the row alone is not enough — the same address would be re-scraped and
    re-contacted tomorrow. The address is also tombstoned into ``blacklist.txt`` so
    ``send_email()`` refuses it permanently.
    """
    settings = settings or get_settings()
    normalised = email.strip().lower()
    await session.execute(delete(Lead).where(Lead.work_email == normalised))

    path = settings.blacklist_path
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if normalised not in existing:
        stamp = _now().isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n{normalised}  # ERASURE requested {stamp}\n")
    log.info("gdpr.erasure", email_domain=normalised.partition("@")[2])


async def leads_for_outreach(
    session: AsyncSession, limit: int = 25, *, product_id: str | None = None
) -> list[Lead]:
    """The oldest LIA-passed leads not yet contacted.

    ``product_id`` scopes to leads sourced for one specific idea -- required for a
    validation run, where the response rate must measure interest in *that* idea,
    not a global pool. Omitted (the default), this is the existing steady-state
    behaviour: the oldest ready leads across every product.

    "Not yet contacted" was previously only a claim in this docstring -- the query
    never actually excluded a lead with prior outreach history, so the same address
    could be re-selected and re-emailed on every subsequent call indefinitely. A
    lead is excluded once it has a ``sent`` ``OutreachLog`` row: a ``blocked``
    (content-policy) or ``suppressed`` (blacklist/transport) attempt never actually
    reached the recipient, so it deliberately remains eligible for a retry.
    """
    already_contacted = (
        select(OutreachLog.lead_id)
        .where(OutreachLog.lead_id == Lead.id, OutreachLog.status == "sent")
        .exists()
    )
    stmt = select(Lead).where(Lead.lia_passed.is_(True), ~already_contacted)
    if product_id is not None:
        stmt = stmt.where(Lead.product_id == product_id)
    stmt = stmt.order_by(Lead.created_at.asc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def count_leads(session: AsyncSession, product_id: str) -> int:
    stmt = select(func.count()).select_from(Lead).where(Lead.product_id == product_id)
    return int((await session.execute(stmt)).scalar_one())


async def insert_validation_signal(
    session: AsyncSession,
    *,
    product_id: str,
    email: str,
    message: str | None = None,
    variant: str | None = None,
    require_confirmation: bool = True,
    confirmation_token: str | None = None,
    submitter_ip_hash: str | None = None,
) -> str | None:
    """Record real interest in a product still in its validation stage.

    ``ON CONFLICT DO NOTHING`` on ``(product_id, email)`` -- a repeat submission
    from the same address must never inflate the count a build-or-scrap decision is
    gated on. Returns ``None`` on a duplicate, matching this codebase's existing
    idempotent-insert idiom (`enqueue_task`, `open_interrupt`). ``variant`` is
    whatever AB-test arm the submitting page reported showing -- not verified
    against a currently-running test here, since a stray or late report from a
    test that just concluded is still a real signal worth keeping, just not one
    `tally_ab_test_outcomes` will count once the test's row is no longer `running`.

    ``require_confirmation=False`` auto-confirms the row at insert time
    (``confirmation_method='dry_run_auto'``) rather than branching the read side
    on a mode flag -- ``tally_validation_signals`` stays a single, honest query
    (``confirmed_at IS NOT NULL``) regardless of why a row counts. The caller
    (the ``/interest`` endpoint) decides ``require_confirmation`` from
    ``settings.factory_dry_run``, not this function.
    """
    confirmed_at = None if require_confirmation else _now()
    method = None if require_confirmation else "dry_run_auto"
    stmt = (
        pg_insert(ValidationSignal)
        .values(
            product_id=product_id,
            email=email.strip().lower(),
            message=message,
            variant=variant,
            confirmed_at=confirmed_at,
            confirmation_token=confirmation_token if require_confirmation else None,
            confirmation_method=method,
            submitter_ip_hash=submitter_ip_hash,
        )
        .on_conflict_do_nothing(
            index_elements=[ValidationSignal.product_id, ValidationSignal.email]
        )
        .returning(ValidationSignal.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def confirm_validation_signal(session: AsyncSession, token: str) -> str | None:
    """Resolve a double-opt-in confirmation click. Idempotent via ``coalesce``,
    not a ``WHERE confirmed_at IS NULL`` guard: a second click still matches the
    row and re-runs the UPDATE, but ``confirmed_at``/``confirmation_method``
    only ever move from unset to their first-click value, never overwritten on
    a later click. Same email comes back either way, so a user double-clicking
    or a mail client prefetching the link can't raise an error.

    Returns the confirmed row's email (for ``/u/{token}`` to also resolve this
    token as an erasure route -- see ``api/app.py::unsubscribe``), or ``None``
    if the token is unknown.
    """
    stmt = (
        update(ValidationSignal)
        .where(ValidationSignal.confirmation_token == token)
        .values(
            confirmed_at=func.coalesce(ValidationSignal.confirmed_at, _now()),
            confirmation_method=case(
                (ValidationSignal.confirmed_at.is_(None), "double_opt_in"),
                else_=ValidationSignal.confirmation_method,
            ),
        )
        .returning(ValidationSignal.email)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def validation_signal_email_for_token(session: AsyncSession, token: str) -> str | None:
    """Look up the address a confirmation token belongs to, without mutating
    anything -- used by ``/u/{token}`` erasure, which must resolve the token
    whether or not it was ever clicked."""
    stmt = select(ValidationSignal.email).where(ValidationSignal.confirmation_token == token)
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def tally_validation_signals(
    session: AsyncSession,
    product_id: str,
    *,
    variant: str | None = None,
    confirmed_only: bool = True,
) -> int:
    stmt = (
        select(func.count())
        .select_from(ValidationSignal)
        .where(ValidationSignal.product_id == product_id)
    )
    if confirmed_only:
        stmt = stmt.where(ValidationSignal.confirmed_at.isnot(None))
    if variant is not None:
        stmt = stmt.where(ValidationSignal.variant == variant)
    return int((await session.execute(stmt)).scalar_one())


async def validation_signal_ip_concentration(
    session: AsyncSession, product_id: str
) -> tuple[int, int]:
    """``(max signals sharing one submitter IP hash, total confirmed signals)``
    for a product -- what ``_evaluate_validation`` checks before trusting a
    response rate as broad real interest rather than one submitter clicking
    through several times.

    Rows with no IP hash (``ABUSE_IP_HASH_SECRET`` unset) never contribute to
    the concentration count -- an unhashed row is not evidence either way, and
    counting it would make the check either meaningless or a false positive
    depending on how NULLs happened to group.
    """
    total = await tally_validation_signals(session, product_id)
    if total == 0:
        return 0, 0
    stmt = (
        select(func.count())
        .select_from(ValidationSignal)
        .where(
            ValidationSignal.product_id == product_id,
            ValidationSignal.confirmed_at.isnot(None),
            ValidationSignal.submitter_ip_hash.isnot(None),
        )
        .group_by(ValidationSignal.submitter_ip_hash)
        .order_by(func.count().desc())
        .limit(1)
    )
    max_from_one_ip = (await session.execute(stmt)).scalar_one_or_none()
    return int(max_from_one_ip or 0), total


async def expire_unconfirmed_validation_signals(
    session: AsyncSession, *, older_than: timedelta
) -> int:
    """Hard-delete validation signals that were never confirmed and have aged
    past the confirmation window -- data minimisation (an unconfirmed row is an
    unverified claim about a real person's email, not evidence), and it keeps
    the table from filling with abandoned or hostile submissions that will
    never count for anything."""
    cutoff = _now() - older_than
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            delete(ValidationSignal).where(
                ValidationSignal.confirmed_at.is_(None), ValidationSignal.created_at < cutoff
            )
        ),
    )
    return int(result.rowcount or 0)


async def purge_rate_limit_buckets(session: AsyncSession, *, older_than: timedelta) -> int:
    """Drop rate-limit windows old enough that they can no longer affect any
    live decision -- pure housekeeping, the table is otherwise unbounded."""
    cutoff = _now() - older_than
    result = cast(
        "CursorResult[Any]",
        await session.execute(delete(RateLimitBucket).where(RateLimitBucket.window_start < cutoff)),
    )
    return int(result.rowcount or 0)


async def record_page_view(
    session: AsyncSession,
    *,
    product_id: str,
    path: str,
    referrer: str | None = None,
    variant: str | None = None,
) -> None:
    """A page view is anonymous by design -- no dedup, no identity, unlike
    `insert_validation_signal`'s per-email upsert. The caller (the public
    `/analytics/{product_id}` endpoint) checks the product exists first and 404s
    before this is ever reached, matching `/interest/{product_id}`'s pattern --
    this function assumes that check already happened and lets the FK raise if it
    somehow didn't.
    """
    stmt = pg_insert(PageView).values(
        product_id=product_id,
        path=path,
        referrer=referrer,
        variant=variant,
    )
    await session.execute(stmt)


async def tally_page_views(
    session: AsyncSession,
    product_id: str,
    *,
    since: datetime | None = None,
    variant: str | None = None,
) -> int:
    stmt = select(func.count()).select_from(PageView).where(PageView.product_id == product_id)
    if since is not None:
        stmt = stmt.where(PageView.created_at >= since)
    if variant is not None:
        stmt = stmt.where(PageView.variant == variant)
    return int((await session.execute(stmt)).scalar_one())


async def count_page_views_from_referrer(
    session: AsyncSession, product_id: str, *, since: datetime, referrer_contains: str
) -> int:
    """Best-effort measurement for a `ChannelExperiment` -- how many visits since it
    was approved carried a referrer naming the channel's own domain. Best-effort
    because `referrer` is whatever the visitor's browser sent (or omitted); this is
    a growth signal, never something a spend or compliance decision is gated on."""
    stmt = (
        select(func.count())
        .select_from(PageView)
        .where(
            PageView.product_id == product_id,
            PageView.created_at >= since,
            PageView.referrer.ilike(f"%{referrer_contains}%"),
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def create_ab_test(
    session: AsyncSession, *, product_id: str, variant_a_brief: str, variant_b_brief: str
) -> str | None:
    """Start a live split test. Returns ``None`` if this product already has one
    running -- the partial unique index on ``(product_id) WHERE status='running'``
    (migration `0013`) is what actually enforces this; a caller racing another
    worker to start a test gets a clean no-op here, not a constraint violation.

    ``index_where`` must be a literal ``text()`` clause, not an ORM comparison
    (``ABTest.status == "running"``) -- the latter compiles to a bound parameter
    (``status = $1``), and Postgres only matches an ``ON CONFLICT ... WHERE``
    predicate against a partial index by comparing parsed expression trees: a
    ``Param`` node never structurally matches the index's own ``Const`` node,
    regardless of the runtime value, so every insert raised
    ``InvalidColumnReferenceError`` instead of ever hitting the no-op path. Caught
    by this phase's own integration tests exercising the conflict for the first
    time; the identical pre-existing pattern in `create_marketing_freeze` had the
    same bug, unexercised by any test until fixed alongside this one.
    """
    stmt = (
        pg_insert(ABTest)
        .values(
            product_id=product_id,
            variant_a_brief=variant_a_brief,
            variant_b_brief=variant_b_brief,
        )
        .on_conflict_do_nothing(
            index_elements=[ABTest.product_id],
            index_where=text("status = 'running'"),
        )
        .returning(ABTest.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def get_running_ab_test(session: AsyncSession, product_id: str) -> ABTest | None:
    stmt = select(ABTest).where(ABTest.product_id == product_id, ABTest.status == "running")
    return (await session.execute(stmt)).scalar_one_or_none()


async def running_ab_tests(session: AsyncSession, *, limit: int = 20) -> list[ABTest]:
    """Every test still open, oldest first -- what a periodic sweep evaluates."""
    stmt = (
        select(ABTest)
        .where(ABTest.status == "running")
        .order_by(ABTest.started_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def conclude_ab_test(session: AsyncSession, ab_test_id: str, *, winner: str) -> None:
    await session.execute(
        update(ABTest)
        .where(ABTest.id == ab_test_id)
        .values(status="concluded", winner=winner, concluded_at=_now())
    )


async def tally_ab_test_outcomes(
    session: AsyncSession, product_id: str, *, since: datetime
) -> dict[str, tuple[int, int]]:
    """Per-variant ``(views, signals)`` since the test started, for both arms.

    Two tables, not one: `page_views` is every visit (the denominator),
    `validation_signals` is the subset that actually converted (the numerator) --
    the same relationship `_evaluate_validation` already uses for the sequential
    validation gate, just split by arm here instead of summed across the product.
    """
    result: dict[str, tuple[int, int]] = {}
    for variant in ("a", "b"):
        views = await tally_page_views(session, product_id, since=since, variant=variant)
        signals = await tally_validation_signals(session, product_id, variant=variant)
        result[variant] = (views, signals)
    return result


async def count_outreach_sent(session: AsyncSession, product_id: str) -> int:
    stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .where(OutreachLog.product_id == product_id, OutreachLog.status == "sent")
    )
    return int((await session.execute(stmt)).scalar_one())


async def erase_validation_signals(session: AsyncSession, email: str) -> None:
    """GDPR Art. 17 erasure, extended to validation-stage interest submissions --
    a prospect-initiated form fill is still personal data. Called alongside
    `erase_lead`, not as a replacement for it (the two tables are unrelated)."""
    normalised = email.strip().lower()
    await session.execute(delete(ValidationSignal).where(ValidationSignal.email == normalised))


async def record_outreach(
    session: AsyncSession,
    *,
    lead_id: str,
    subject: str,
    status: str,
    product_id: str | None = None,
    suppressed_reason: str | None = None,
    id: str | None = None,
    unsubscribe_token: str | None = None,
    sender_identity_version_id: str | None = None,
) -> str:
    """``id`` may be pre-generated by the caller (see `marketing/outreach.py`) so the
    same id can be handed to the deliverability provider as a tracking variable
    *before* this row exists — its webhook needs something to correlate back to.

    ``unsubscribe_token`` must likewise be pre-generated by the caller whenever an
    email is actually going out: the token embedded in the sent message and the
    token stored here must be the same value, or `GET /u/{token}` can never resolve
    it. Only generate one here when the caller has none to give (e.g. a `blocked`
    row that was never sent to anyone).

    ``sender_identity_version_id`` should only ever be passed for a ``status="sent"``
    row — see `marketing/outreach.py::send_to_lead`."""
    entry = OutreachLog(
        id=id or new_id(),
        lead_id=lead_id,
        product_id=product_id,
        subject=subject[:500],
        status=status,
        suppressed_reason=suppressed_reason,
        unsubscribe_token=unsubscribe_token or secrets.token_urlsafe(32),
        sender_identity_version_id=sender_identity_version_id,
    )
    session.add(entry)
    await session.flush()
    return entry.id


async def outreach_count_for_domain(session: AsyncSession, domain: str, days: int = 30) -> int:
    since = _now() - timedelta(days=days)
    stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .join(Lead, Lead.id == OutreachLog.lead_id)
        .where(Lead.work_email.like(f"%@{domain}"), OutreachLog.sent_at >= since)
    )
    return int((await session.execute(stmt)).scalar_one())


async def first_revenue_ledger_entries_needing_attribution(
    session: AsyncSession,
) -> list[LedgerEntry]:
    """The earliest ``revenue`` ledger row per attributable product, excluding any
    already claimed by an outreach row. A renewal (a product's *second* revenue row)
    is deliberately never a candidate — it isn't a new "outreach worked" signal."""
    first_per_product = (
        select(
            LedgerEntry.product_id.label("product_id"),
            func.min(LedgerEntry.created_at).label("first_at"),
        )
        .where(LedgerEntry.kind == "revenue", LedgerEntry.product_id.is_not(None))
        .group_by(LedgerEntry.product_id)
        .subquery()
    )
    stmt = (
        select(LedgerEntry)
        .join(
            first_per_product,
            (LedgerEntry.product_id == first_per_product.c.product_id)
            & (LedgerEntry.created_at == first_per_product.c.first_at),
        )
        .outerjoin(OutreachLog, OutreachLog.ledger_entry_id == LedgerEntry.id)
        .where(OutreachLog.id.is_(None))
    )
    return list((await session.execute(stmt)).scalars().all())


async def last_outreach_touch(
    session: AsyncSession, *, product_id: str, before: datetime, window_days: int
) -> OutreachLog | None:
    """The most recent successful send for this product inside the attribution
    window — last touch, not first: with no buyer identity, first-touch would let an
    early send in a multi-touch sequence hog credit for a conversion it may be
    unrelated to."""
    since = before - timedelta(days=window_days)
    stmt = (
        select(OutreachLog)
        .where(
            OutreachLog.product_id == product_id,
            OutreachLog.status == "sent",
            OutreachLog.sent_at >= since,
            OutreachLog.sent_at <= before,
        )
        .order_by(OutreachLog.sent_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def attribute_outreach(
    session: AsyncSession, *, outreach_log_id: str, ledger_entry_id: str, converted_at: datetime
) -> None:
    await session.execute(
        update(OutreachLog)
        .where(OutreachLog.id == outreach_log_id)
        .values(ledger_entry_id=ledger_entry_id, converted_at=converted_at)
    )


# ------------------------------------------------------------------------- ledger

#: Arbitrary fixed key for a Postgres session-transaction advisory lock (see
#: `append_ledger`) -- any bigint works, it just has to be the same constant every
#: call. Not tied to any row or table, since there is nothing to `SELECT ... FOR
#: UPDATE` ahead of an INSERT that doesn't exist yet.
_LEDGER_LOCK_KEY = 0x4C454447_45_5F31  # "LEDG_1" in hex, arbitrary but stable


async def current_balance(session: AsyncSession) -> tuple[int, int]:
    """(balance_cents, reinvest_pool_cents) as of the latest entry."""
    stmt = select(LedgerEntry).order_by(LedgerEntry.created_at.desc()).limit(1)
    entry = (await session.execute(stmt)).scalar_one_or_none()
    if entry is None:
        return 0, 0
    return entry.balance_cents, entry.reinvest_pool_cents


async def latest_ledger_entry(session: AsyncSession) -> LedgerEntry | None:
    """The real recency signal behind the dashboard's "last ledger write"
    display -- there is no Stripe/ledger sync-state concept anywhere in this
    codebase to report instead."""
    stmt = select(LedgerEntry).order_by(LedgerEntry.created_at.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def ledger_amounts_by_kind(
    session: AsyncSession, *, kind: str, since: datetime
) -> list[tuple[datetime, int]]:
    """(created_at, amount_cents) for every real ledger row of ``kind`` at or
    after ``since``, oldest first -- the raw series
    `finance/anomaly.py::detect_anomaly` buckets into daily totals itself,
    rather than pushing date-bucketing (and its per-database-dialect syntax)
    into SQL."""
    stmt = (
        select(LedgerEntry.created_at, LedgerEntry.amount_cents)
        .where(LedgerEntry.kind == kind, LedgerEntry.created_at >= since)
        .order_by(LedgerEntry.created_at.asc())
    )
    return [(row.created_at, row.amount_cents) for row in (await session.execute(stmt)).all()]


async def append_ledger(
    session: AsyncSession,
    *,
    external_event_id: str,
    kind: str,
    amount_cents: int,
    reinvest_delta_cents: int = 0,
    product_id: str | None = None,
    stripe_subscription_id: str | None = None,
    stripe_customer_id: str | None = None,
    memo: str | None = None,
    currency: str = "USD",
) -> bool:
    """Append one immutable money event. Idempotent on the provider's event id.

    Returns False when the event was already recorded, so a replayed Stripe webhook
    can never double-count revenue.

    ``balance_cents``/``reinvest_pool_cents`` are a running total derived by reading
    the latest row and adding this event's delta -- there is no separate counter row
    to ``SELECT ... FOR UPDATE``. Two webhook deliveries handled concurrently (the
    API and the scheduler share one event loop, so this is a real interleaving, not
    just a multi-process race) would otherwise both read the same stale balance and
    both commit from it, silently losing one event's contribution forever. A
    Postgres transaction-scoped advisory lock serializes the read-then-write: the
    second caller blocks here until the first commits (releasing the lock
    automatically) and only then reads the now-correct balance.
    """
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LEDGER_LOCK_KEY})
    balance, pool = await current_balance(session)
    new_balance = balance + amount_cents
    new_pool = pool + reinvest_delta_cents
    if new_balance < 0:
        raise ValueError(
            f"refusing ledger entry: balance would become {new_balance} cents (negative)"
        )
    if new_pool < 0:
        raise ValueError(f"refusing ledger entry: reinvest pool would become {new_pool} cents")

    stmt = (
        pg_insert(LedgerEntry)
        .values(
            external_event_id=external_event_id,
            kind=kind,
            amount_cents=amount_cents,
            balance_cents=new_balance,
            reinvest_pool_cents=new_pool,
            currency=currency,
            product_id=product_id,
            stripe_subscription_id=stripe_subscription_id,
            stripe_customer_id=stripe_customer_id,
            memo=memo,
        )
        .on_conflict_do_nothing(index_elements=[LedgerEntry.external_event_id])
        .returning(LedgerEntry.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


# --------------------------------------------------------------------- subscriptions
#
# None of these functions may ever call append_ledger -- subscription lifecycle
# events move no money (see finance/webhooks.py's own module docstring); the
# real transactions (invoice.paid etc.) are already handled by the revenue path
# above. Enforced as an asserted test invariant, not just this comment -- see
# test_subscription_lifecycle.py.


async def upsert_customer(
    session: AsyncSession, *, stripe_customer_id: str, product_id: str | None = None
) -> str:
    """Idempotent on the Stripe customer id -- a customer only ever needs to be
    created once, lazily, on whichever subscription event happens to arrive
    first (Stripe guarantees the Customer object itself already exists by the
    time any Subscription references it, unlike webhook *delivery* order across
    different event types)."""
    stmt = (
        pg_insert(Customer)
        .values(stripe_customer_id=stripe_customer_id, product_id=product_id)
        .on_conflict_do_nothing(index_elements=[Customer.stripe_customer_id])
        .returning(Customer.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return str(row)
    existing = (
        await session.execute(
            select(Customer.id).where(Customer.stripe_customer_id == stripe_customer_id)
        )
    ).scalar_one()
    return str(existing)


async def get_subscription_by_stripe_id(
    session: AsyncSession, stripe_subscription_id: str
) -> Subscription | None:
    stmt = select(Subscription).where(Subscription.stripe_subscription_id == stripe_subscription_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_subscription(
    session: AsyncSession,
    *,
    stripe_subscription_id: str,
    customer_id: str,
    product_id: str | None,
    status: str,
    price_cents: int,
    cancel_at_period_end: bool,
    current_period_end: datetime | None,
) -> str:
    """`customer.subscription.created` and `.updated` both land here -- never
    touches `churn_kind`/`churned_at`, which only `mark_subscription_churned`
    may set. `.updated` refreshing `price_cents`/`status` on every delivery is
    deliberate, not incidental: without it, a plan change or any other update
    reason would leave `mrr_cents` silently stale."""
    stmt = (
        pg_insert(Subscription)
        .values(
            stripe_subscription_id=stripe_subscription_id,
            customer_id=customer_id,
            product_id=product_id,
            status=status,
            price_cents=price_cents,
            cancel_at_period_end=cancel_at_period_end,
            current_period_end=current_period_end,
        )
        .on_conflict_do_update(
            index_elements=[Subscription.stripe_subscription_id],
            set_={
                "status": status,
                "price_cents": price_cents,
                "cancel_at_period_end": cancel_at_period_end,
                "current_period_end": current_period_end,
                "updated_at": func.now(),
            },
        )
        .returning(Subscription.id)
    )
    return str((await session.execute(stmt)).scalar_one())


async def mark_subscription_churned(
    session: AsyncSession, *, stripe_subscription_id: str, status: str, churn_kind: str
) -> None:
    """The only function allowed to set `churn_kind`/`churned_at` -- called
    exclusively from the `customer.subscription.deleted` handler, never from
    `.updated` (which only ever means `cancel_at_period_end` was toggled, a
    still-reversible scheduled cancellation, not an actual churn event yet)."""
    await session.execute(
        update(Subscription)
        .where(Subscription.stripe_subscription_id == stripe_subscription_id)
        .values(status=status, churn_kind=churn_kind, churned_at=func.now())
    )


async def open_or_bump_dunning(
    session: AsyncSession, *, stripe_invoice_id: str, subscription_id: str
) -> None:
    """`invoice.payment_failed` fires once per Stripe retry attempt for the
    *same* invoice -- upserts the same row, incrementing `attempt_count`,
    rather than creating a new row per delivery."""
    stmt = (
        pg_insert(DunningEvent)
        .values(
            stripe_invoice_id=stripe_invoice_id,
            subscription_id=subscription_id,
            status="open",
            attempt_count=1,
        )
        .on_conflict_do_update(
            index_elements=[DunningEvent.stripe_invoice_id],
            set_={"attempt_count": DunningEvent.attempt_count + 1},
        )
    )
    await session.execute(stmt)


async def close_dunning_by_invoice(
    session: AsyncSession, *, stripe_invoice_id: str, reason: str
) -> bool:
    """Closes this invoice's own dunning row on recovery (`invoice.paid`).
    Returns whether an open row was actually found -- most paid invoices never
    failed at all, so "nothing to close" is the routine case, not an error."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(DunningEvent)
            .where(
                DunningEvent.stripe_invoice_id == stripe_invoice_id,
                DunningEvent.status == "open",
            )
            .values(status="recovered", closed_at=func.now(), closed_reason=reason)
        ),
    )
    return result.rowcount > 0


async def close_open_dunning_for_subscription(
    session: AsyncSession, *, subscription_id: str, reason: str
) -> int:
    """Closes any still-open dunning row(s) for a subscription that just
    reached a terminal state -- the zombie-row fix: without this, a customer
    who never recovers never fires `invoice.paid`, and the row would otherwise
    leak open forever."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(DunningEvent)
            .where(DunningEvent.subscription_id == subscription_id, DunningEvent.status == "open")
            .values(status="terminal", closed_at=func.now(), closed_reason=reason)
        ),
    )
    return result.rowcount


async def mrr_cents(session: AsyncSession) -> int:
    stmt = select(func.coalesce(func.sum(Subscription.price_cents), 0)).where(
        Subscription.status == "active"
    )
    return int((await session.execute(stmt)).scalar_one())


async def mrr_by_product(session: AsyncSession) -> dict[str, int]:
    """Per-product breakdown of `mrr_cents` above, which only ever gave the
    global total -- the dashboard's portfolio view needs the split."""
    stmt = (
        select(Subscription.product_id, func.coalesce(func.sum(Subscription.price_cents), 0))
        .where(Subscription.status == "active", Subscription.product_id.is_not(None))
        .group_by(Subscription.product_id)
    )
    return {
        str(product_id): int(total) for product_id, total in (await session.execute(stmt)).all()
    }


async def active_subscription_count(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(Subscription).where(Subscription.status == "active")
    return int((await session.execute(stmt)).scalar_one())


async def churn_counts_since(session: AsyncSession, *, since: datetime) -> tuple[int, int]:
    """``(voluntary, involuntary)`` churn counts since ``since`` -- returned
    together since every caller that wants one plausibly wants both."""
    stmt = (
        select(Subscription.churn_kind, func.count())
        .where(Subscription.churned_at.is_not(None), Subscription.churned_at >= since)
        .group_by(Subscription.churn_kind)
    )
    counts = {kind: int(count) for kind, count in (await session.execute(stmt)).all()}
    return counts.get("voluntary", 0), counts.get("involuntary", 0)


async def churn_rate(session: AsyncSession, *, since: datetime) -> float:
    """A simplified approximation, not a textbook cohort-based calculation: of
    every subscription active right now plus everyone who churned in the
    window, what fraction churned. `ideas`' own per-dimension columns (Phase 4)
    and this metric share the same honest limitation -- good enough at the
    volumes this factory will plausibly see for a long time, not a promise of
    statistical rigor."""
    voluntary, involuntary = await churn_counts_since(session, since=since)
    churned = voluntary + involuntary
    active = await active_subscription_count(session)
    denominator = active + churned
    return round(churned / denominator, 4) if denominator else 0.0


async def churning_products(session: AsyncSession, *, since: datetime) -> list[str]:
    """Distinct product ids with at least one churned subscription (either
    kind) since ``since`` -- feeds ``FactorySnapshot.churning_products``, the
    Orchestrator's own "something is bleeding customers" sense."""
    stmt = (
        select(Subscription.product_id)
        .where(
            Subscription.product_id.is_not(None),
            Subscription.churned_at.is_not(None),
            Subscription.churned_at >= since,
        )
        .distinct()
    )
    return [str(pid) for pid in (await session.execute(stmt)).scalars().all()]


async def unremembered_churned_subscriptions(session: AsyncSession) -> list[Subscription]:
    """Churned subscriptions `finance/churn.py::write_churn_lessons` has not
    yet written a memory for -- see `Subscription.memory_written_at`'s own
    comment for why this is a one-shot marker, not a rolling time window."""
    stmt = select(Subscription).where(
        Subscription.churn_kind.is_not(None), Subscription.memory_written_at.is_(None)
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_churn_memory_written(session: AsyncSession, subscription_id: str) -> None:
    await session.execute(
        update(Subscription)
        .where(Subscription.id == subscription_id)
        .values(memory_written_at=func.now())
    )


# --------------------------------------------------------------------- interrupts


async def open_interrupt(
    session: AsyncSession,
    *,
    reason: InterruptReason,
    subject: str,
    detail: str,
    headline: str,
    reply_hint: str,
    needs_reply: bool = True,
    task_id: str | None = None,
    product_id: str | None = None,
) -> str | None:
    """Raise a human escalation. Duplicate open subjects collapse into one.

    `index_where` targets the partial unique index (`orm.py::Interrupt`,
    scoped to status='open') specifically -- without it, Postgres cannot match
    this INSERT's conflict target to a *partial* index at all, and the
    statement would fail with "there is no unique or exclusion constraint
    matching the ON CONFLICT specification" rather than silently doing the
    wrong thing, but it still has to be told explicitly.

    `headline`/`reply_hint` are required, not optional -- see
    `hitl/formatting.py`'s module docstring for why a fixed per-`reason` label
    was misleading and every raise site now supplies its own.
    """
    stmt = (
        pg_insert(Interrupt)
        .values(
            reason=reason.value,
            subject=subject[:200],
            detail=detail,
            headline=headline,
            reply_hint=reply_hint,
            needs_reply=needs_reply,
            task_id=task_id,
            product_id=product_id,
            status="open",
        )
        .on_conflict_do_nothing(
            index_elements=[Interrupt.reason, Interrupt.subject, Interrupt.status],
            index_where=text("status = 'open'"),
        )
        .returning(Interrupt.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def undelivered_interrupts(session: AsyncSession) -> list[Interrupt]:
    stmt = select(Interrupt).where(Interrupt.status == "open", Interrupt.delivered.is_(False))
    return list((await session.execute(stmt)).scalars().all())


async def count_open_interrupts(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(Interrupt).where(Interrupt.status == "open")
    return int((await session.execute(stmt)).scalar_one())


async def has_open_interrupt_for_product(session: AsyncSession, product_id: str) -> bool:
    """Whether a human is already being asked about this product -- checked
    before raising a new automatic escalation for it (e.g. a diagnosed
    lead-sourcing credential problem, see marketing/graph.py) so nothing piles
    a second one onto a pending decision, and a persistently broken credential
    does not re-escalate on every subsequent harvest cycle."""
    stmt = (
        select(func.count())
        .select_from(Interrupt)
        .where(Interrupt.product_id == product_id, Interrupt.status == "open")
    )
    return int((await session.execute(stmt)).scalar_one()) > 0


async def list_open_interrupts(session: AsyncSession, limit: int = 10) -> list[Interrupt]:
    """Grouped by product, oldest first within each group -- same-product
    escalations read together rather than interleaved with unrelated ones, and
    the ones a human has been waiting longest on still surface first if the
    list is capped. Non-product-scoped escalations sort last."""
    stmt = (
        select(Interrupt)
        .where(Interrupt.status == "open")
        .order_by(Interrupt.product_id.asc().nulls_last(), Interrupt.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def find_open_interrupt_by_tag(session: AsyncSession, tag: str) -> Interrupt | None:
    """Same short-tag scheme as `hitl.interrupts.tag_for` (an interrupt id's first
    8 characters) -- used by `/issue` (`hitl/commands.py`) to look up one specific
    escalation's full detail, independent of `interrupts.py`'s own identical
    reply-matching lookup (`_find_open_by_tag`) so `commands.py` never has to
    import from the module that already imports it."""
    needle = tag.lower()
    stmt = select(Interrupt).where(Interrupt.status == "open")
    for row in (await session.execute(stmt)).scalars().all():
        if row.id.lower().startswith(needle):
            return row
    return None


async def stale_open_interrupts(session: AsyncSession, *, older_than: timedelta) -> list[Interrupt]:
    """Interrupts still waiting on a human past ``older_than`` since they were raised.
    Grouped by product, oldest first within each group -- same reasoning as
    `list_open_interrupts`.

    Delivery already happened by the time something qualifies here (see
    ``undelivered_interrupts``) — this is for the case the human missed or forgot the
    original message, not the case the message never arrived.
    """
    cutoff = _now() - older_than
    stmt = (
        select(Interrupt)
        .where(Interrupt.status == "open", Interrupt.created_at < cutoff)
        .order_by(Interrupt.product_id.asc().nulls_last(), Interrupt.created_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_interrupt_delivered(session: AsyncSession, interrupt_id: str) -> None:
    await session.execute(
        update(Interrupt).where(Interrupt.id == interrupt_id).values(delivered=True)
    )


async def resolve_open_interrupts_by_subject(
    session: AsyncSession, *, reason: InterruptReason, subject: str, response: str
) -> int:
    """Close every open interrupt matching one (reason, subject), returning how many.

    For a condition that clears on its own and should stop being reported the
    moment it does. A provider outage is the case this exists for: the breaker
    self-heals from real call outcomes, but the *notice* raised when it tripped
    outlived it by hours and kept telling an operator a provider was down that had
    long since recovered -- worse than no notice, because it is confidently wrong.
    """
    stmt = (
        update(Interrupt)
        .where(
            Interrupt.reason == reason.value,
            Interrupt.subject == subject[:200],
            Interrupt.status == "open",
        )
        .values(status="resolved", response=response, resolved_at=_now())
        .returning(Interrupt.id)
    )
    return len((await session.execute(stmt)).scalars().all())


async def resolve_interrupt(session: AsyncSession, interrupt_id: str, response: str) -> None:
    await session.execute(
        update(Interrupt)
        .where(Interrupt.id == interrupt_id)
        .values(status="resolved", response=response, resolved_at=_now())
    )


# ------------------------------------------------------------- integration alerts


async def raise_integration_alert(
    session: AsyncSession, *, provider: str, message: str
) -> str | None:
    """Record a usage-limit/quota/account signal from a third-party integration.

    Same collapse-into-the-existing-open-row shape as `open_interrupt` above,
    and for the same reason: a provider that keeps hitting the same limit on
    every retry must not spam a fresh row each time. Returns `None` when an
    identical (provider, message) alert is already open -- the caller does
    not need to distinguish "raised" from "already known," see
    `hitl.interrupts.request`'s identical shrug at its own `None` return.
    """
    stmt = (
        pg_insert(IntegrationAlert)
        .values(provider=provider, message=message[:2000], status="open")
        .on_conflict_do_nothing(
            index_elements=[
                IntegrationAlert.provider,
                IntegrationAlert.message,
                IntegrationAlert.status,
            ],
            index_where=text("status = 'open'"),
        )
        .returning(IntegrationAlert.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    return str(row) if row is not None else None


async def list_open_integration_alerts(
    session: AsyncSession, *, limit: int = 50
) -> list[IntegrationAlert]:
    stmt = (
        select(IntegrationAlert)
        .where(IntegrationAlert.status == "open")
        .order_by(IntegrationAlert.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_open_integration_alerts(session: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(IntegrationAlert)
        .where(IntegrationAlert.status == "open")
    )
    return int((await session.execute(stmt)).scalar_one())


async def create_custom_credential(session: AsyncSession, key: str) -> str | None:
    """Register a dashboard-added credential key. Idempotent, same shape as
    `enqueue_task`'s upsert: re-adding an already-known key (the value may
    have just been updated in `.env`) is a no-op here, not a duplicate row --
    `None` return just means "already registered", not an error."""
    stmt = (
        pg_insert(CustomCredential)
        .values(key=key)
        .on_conflict_do_nothing(index_elements=[CustomCredential.key])
        .returning(CustomCredential.id)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    return str(row) if row is not None else None


async def list_custom_credentials(session: AsyncSession) -> list[CustomCredential]:
    stmt = select(CustomCredential).order_by(CustomCredential.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def resolve_integration_alert(session: AsyncSession, alert_id: str) -> bool:
    """True if an open alert with this id existed and was resolved -- lets the
    dashboard endpoint 404 on a stale/unknown id instead of silently no-op'ing,
    same reasoning as `api/dashboard.py::resolve_interrupt`."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(IntegrationAlert)
            .where(IntegrationAlert.id == alert_id, IntegrationAlert.status == "open")
            .values(status="resolved", resolved_at=_now())
        ),
    )
    return result.rowcount > 0


# ---------------------------------------------------------------------- llm calls


# ----------------------------------------------------------- campaign approvals


async def get_or_create_campaign_approval(
    session: AsyncSession,
    *,
    product_id: str | None,
    task_id: str,
    lead_count: int,
    brief: str,
) -> CampaignApproval:
    """Idempotent on ``task_id``: the first call for a given send-task cohort creates
    the row and raises the interrupt (via the caller); every later call — this task
    retried on a subsequent tick, still pending — returns the same row unchanged."""
    stmt = (
        pg_insert(CampaignApproval)
        .values(product_id=product_id, task_id=task_id, lead_count=lead_count, brief=brief)
        .on_conflict_do_nothing(index_elements=[CampaignApproval.task_id])
    )
    await session.execute(stmt)
    existing = (
        await session.execute(select(CampaignApproval).where(CampaignApproval.task_id == task_id))
    ).scalar_one()
    return existing


async def set_campaign_approval_interrupt(
    session: AsyncSession, campaign_approval_id: str, interrupt_id: str
) -> None:
    await session.execute(
        update(CampaignApproval)
        .where(CampaignApproval.id == campaign_approval_id)
        .values(interrupt_id=interrupt_id)
    )


async def pending_campaign_approvals(session: AsyncSession) -> list[CampaignApproval]:
    stmt = select(CampaignApproval).where(
        CampaignApproval.status == "pending", CampaignApproval.interrupt_id.is_not(None)
    )
    return list((await session.execute(stmt)).scalars().all())


async def decide_campaign_approval(
    session: AsyncSession, campaign_approval_id: str, decision: str
) -> None:
    """``decision`` is always ``'approved'`` or ``'rejected'`` — the caller
    (`marketing.campaign.sync_decisions`) fails closed on anything ambiguous before
    reaching here, so this function itself trusts the value rather than re-validating
    it."""
    await session.execute(
        update(CampaignApproval)
        .where(CampaignApproval.id == campaign_approval_id)
        .values(status=decision, decided_at=_now())
    )


# --------------------------------------------------------------- channel experiments


async def create_channel_experiment(
    session: AsyncSession,
    *,
    product_id: str,
    channel: str,
    url: str,
    rationale: str,
    draft_content: str,
) -> str:
    row = ChannelExperiment(
        product_id=product_id,
        channel=channel[:40],
        url=url[:500],
        rationale=rationale[:2000],
        draft_content=draft_content[:6000],
    )
    session.add(row)
    await session.flush()
    return row.id


async def set_channel_experiment_interrupt(
    session: AsyncSession, experiment_id: str, interrupt_id: str
) -> None:
    await session.execute(
        update(ChannelExperiment)
        .where(ChannelExperiment.id == experiment_id)
        .values(interrupt_id=interrupt_id)
    )


async def pending_channel_experiments(session: AsyncSession) -> list[ChannelExperiment]:
    stmt = select(ChannelExperiment).where(
        ChannelExperiment.status == "proposed", ChannelExperiment.interrupt_id.is_not(None)
    )
    return list((await session.execute(stmt)).scalars().all())


async def decide_channel_experiment(
    session: AsyncSession, experiment_id: str, decision: str
) -> None:
    """``decision`` is always ``'approved'`` or ``'rejected'`` -- same fail-closed
    contract as `decide_campaign_approval`, enforced by the caller
    (`marketing.channel.sync_decisions`)."""
    await session.execute(
        update(ChannelExperiment)
        .where(ChannelExperiment.id == experiment_id)
        .values(status=decision, decided_at=_now())
    )


async def approved_channel_experiments_awaiting_conclusion(
    session: AsyncSession, *, older_than: timedelta
) -> list[ChannelExperiment]:
    """Approved experiments whose measurement window has elapsed -- what the
    periodic sweep in scheduler.py turns into a `MEASURE_CHANNEL_EXPERIMENT` task."""
    cutoff = _now() - older_than
    stmt = select(ChannelExperiment).where(
        ChannelExperiment.status == "approved", ChannelExperiment.decided_at < cutoff
    )
    return list((await session.execute(stmt)).scalars().all())


async def conclude_channel_experiment(
    session: AsyncSession, experiment_id: str, *, outcome: str
) -> None:
    await session.execute(
        update(ChannelExperiment)
        .where(ChannelExperiment.id == experiment_id)
        .values(status="concluded", outcome=outcome[:2000], concluded_at=_now())
    )


async def recent_channel_experiments(
    session: AsyncSession, product_id: str, limit: int = 10
) -> list[ChannelExperiment]:
    """What CHANNEL_SCOUT reads before proposing anything new, so it does not
    re-propose a channel already tried (or already awaiting a human decision)."""
    stmt = (
        select(ChannelExperiment)
        .where(ChannelExperiment.product_id == product_id)
        .order_by(ChannelExperiment.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


# ----------------------------------------------------------------- scout signals


async def record_scout_signal(
    session: AsyncSession,
    *,
    task_id: str,
    title: str,
    body: str,
    url: str,
    source: str,
    engagement: int = 0,
) -> str:
    row = ScoutSignal(
        task_id=task_id,
        title=title[:300],
        body=body[:4000],
        url=url[:500],
        source=source[:60],
        engagement=max(0, engagement),
    )
    session.add(row)
    await session.flush()
    return row.id


async def drain_scout_signals(session: AsyncSession, task_id: str) -> list[ScoutSignal]:
    """Read back every signal DISCOVERY_SCOUT submitted for this task, and delete
    them -- this table is a one-shot hand-off buffer, not a durable record (see
    `state/orm.py::ScoutSignal`)."""
    stmt = select(ScoutSignal).where(ScoutSignal.task_id == task_id)
    rows = list((await session.execute(stmt)).scalars().all())
    if rows:
        await session.execute(delete(ScoutSignal).where(ScoutSignal.task_id == task_id))
    return rows


# ---------------------------------------------------------------- data integrity


async def orphaned_approved_ideas(session: AsyncSession) -> list[Idea]:
    """Every idea marked 'approved' should have at least one product --
    Compliance always creates one in the same pass an idea is approved. An
    approved idea with no product at all means that guarantee broke somewhere.

    Deliberately the mirror image of `orchestrator.py::build_snapshot`'s
    `approved_ideas_waiting` query: that one INNER JOINs and filters on product
    status, which silently drops (undercounts, never flags) any approved idea
    with no matching product row at all. This checks the assumption the other
    query just trusts.
    """
    stmt = (
        select(Idea)
        .outerjoin(Product, Product.idea_id == Idea.id)
        .where(Idea.status == "approved", Product.id.is_(None))
    )
    return list((await session.execute(stmt)).scalars().all())


async def most_recent_product_activity(session: AsyncSession) -> datetime | None:
    """The single most recent `Product.updated_at` across every product, or
    `None` if there are no products yet. `supervisor/audit.py`'s business-
    health sweep uses this to notice "nothing has moved in the whole factory
    for days" -- a signal a founder watching the day-to-day would pick up on
    long before it shows up in any per-engine metric."""
    stmt = select(func.max(Product.updated_at))
    return (await session.execute(stmt)).scalar_one_or_none()


async def products_with_recent_heal_attempts(
    session: AsyncSession, *, since: datetime
) -> list[str]:
    """Distinct `product_id`s with at least one `HealAttempt` row at or after
    `since`. Counting distinct *products*, not raw attempt rows, on purpose:
    one product legitimately cycling through several heals is routine, but
    several unrelated products all hitting a heal cycle in the same short
    window is the kind of pattern a human operator would notice and suspect a
    systemic cause for, not four coincidental unrelated bugs."""
    stmt = (
        select(HealAttempt.product_id)
        .where(HealAttempt.created_at >= since)
        .distinct()
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


# --------------------------------------------------------------- entity verdicts


async def get_entity_verdict(session: AsyncSession, email_domain: str) -> EntityVerdictRow | None:
    stmt = select(EntityVerdictRow).where(EntityVerdictRow.email_domain == email_domain)
    return (await session.execute(stmt)).scalar_one_or_none()


async def store_entity_verdict(
    session: AsyncSession,
    *,
    email_domain: str,
    entity_type: str,
    confidence: float,
    evidence: str,
    task_id: str | None = None,
) -> EntityVerdictRow:
    """Idempotent on ``email_domain``: concurrent resolution of the same never-before
    -seen domain (two leads from the same company in one harvest) keeps whichever
    verdict landed first rather than raising or overwriting."""
    stmt = (
        pg_insert(EntityVerdictRow)
        .values(
            email_domain=email_domain,
            entity_type=entity_type,
            confidence=confidence,
            evidence=evidence[:1500],
            task_id=task_id,
        )
        .on_conflict_do_nothing(index_elements=[EntityVerdictRow.email_domain])
    )
    await session.execute(stmt)
    existing = await get_entity_verdict(session, email_domain)
    assert existing is not None
    return existing


# ------------------------------------------------------------ deliverability


async def outreach_log_by_id(session: AsyncSession, outreach_log_id: str) -> OutreachLog | None:
    return await session.get(OutreachLog, outreach_log_id)


async def record_bounce(session: AsyncSession, outreach_log_id: str) -> None:
    await session.execute(
        update(OutreachLog).where(OutreachLog.id == outreach_log_id).values(bounced_at=_now())
    )


async def record_complaint(session: AsyncSession, outreach_log_id: str) -> None:
    await session.execute(
        update(OutreachLog).where(OutreachLog.id == outreach_log_id).values(complained_at=_now())
    )


async def mark_identity_outcome_recorded(session: AsyncSession, outreach_log_id: str) -> None:
    """Stamped only by the sender-identity timeout path (`marketing/attribution.py`)
    so a send that ages past the attribution window is never scored twice."""
    await session.execute(
        update(OutreachLog)
        .where(OutreachLog.id == outreach_log_id)
        .values(identity_outcome_recorded_at=_now())
    )


async def outreach_needing_identity_timeout(
    session: AsyncSession, *, older_than_days: int
) -> list[OutreachLog]:
    """Sent, non-bounced sends that scored a sender-identity version, aged past the
    attribution window with no conversion, and haven't been scored yet. A bounced
    send is excluded — the recipient never saw the name, so a bounce isn't a naming
    failure. A late conversion after this point is impossible: `last_outreach_touch`'s
    own window means a send this old could never be matched to a ledger entry anyway,
    so there's no race with the success path in `attribution.py::reconcile`."""
    cutoff = _now() - timedelta(days=older_than_days)
    stmt = select(OutreachLog).where(
        OutreachLog.status == "sent",
        OutreachLog.sender_identity_version_id.is_not(None),
        OutreachLog.identity_outcome_recorded_at.is_(None),
        OutreachLog.converted_at.is_(None),
        OutreachLog.bounced_at.is_(None),
        OutreachLog.sent_at <= cutoff,
    )
    return list((await session.execute(stmt)).scalars().all())


async def deliverability_counts(
    session: AsyncSession, *, window_days: int = 30
) -> tuple[int, int, int]:
    """(sent, complained, bounced) over the trailing window. The denominator is
    every send attempt regardless of later outcome — a bounce or complaint is
    still a send."""
    since = _now() - timedelta(days=window_days)
    sent_stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .where(OutreachLog.status == "sent", OutreachLog.sent_at >= since)
    )
    complained_stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .where(OutreachLog.complained_at.is_not(None), OutreachLog.sent_at >= since)
    )
    bounced_stmt = (
        select(func.count())
        .select_from(OutreachLog)
        .where(OutreachLog.bounced_at.is_not(None), OutreachLog.sent_at >= since)
    )
    sent = int((await session.execute(sent_stmt)).scalar_one())
    complained = int((await session.execute(complained_stmt)).scalar_one())
    bounced = int((await session.execute(bounced_stmt)).scalar_one())
    return sent, complained, bounced


async def active_marketing_freeze(session: AsyncSession) -> MarketingFreeze | None:
    stmt = select(MarketingFreeze).where(MarketingFreeze.status == "active")
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_marketing_freeze(
    session: AsyncSession, *, reason: str, complaint_rate: float
) -> MarketingFreeze | None:
    """Idempotent: the partial-unique index on ``status='active'`` means a second
    trip while one is already active is a no-op, not a duplicate freeze.

    ``index_where`` is required, not optional, here: Postgres only matches an
    ``ON CONFLICT`` target against a partial index when the predicate is repeated
    verbatim. Without it, ``index_elements=[status]`` alone matches nothing (this
    table has no plain unique constraint on ``status``, only the partial one) and
    Postgres raises ``InvalidColumnReferenceError`` instead of silently ignoring
    the conflict.

    ``index_where`` must be a literal ``text()`` clause, not an ORM comparison --
    the latter compiles to a bound parameter, which Postgres's ON CONFLICT
    inference never matches against the index's own literal predicate (a ``Param``
    node cannot structurally equal a ``Const`` node), so every call raised
    instead of no-opping. See `create_ab_test` for the same bug, caught there
    first by a test that actually exercised the conflict path.
    """
    stmt = (
        pg_insert(MarketingFreeze)
        .values(reason=reason[:4000], complaint_rate=complaint_rate, status="active")
        .on_conflict_do_nothing(
            index_elements=[MarketingFreeze.status],
            index_where=text("status = 'active'"),
        )
        .returning(MarketingFreeze.id)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return await session.get(MarketingFreeze, row)


async def set_marketing_freeze_interrupt(
    session: AsyncSession, freeze_id: str, interrupt_id: str
) -> None:
    await session.execute(
        update(MarketingFreeze)
        .where(MarketingFreeze.id == freeze_id)
        .values(interrupt_id=interrupt_id)
    )


async def clear_marketing_freeze(session: AsyncSession, freeze_id: str) -> None:
    await session.execute(
        update(MarketingFreeze)
        .where(MarketingFreeze.id == freeze_id)
        .values(status="cleared", cleared_at=_now())
    )


async def record_llm_call(
    session: AsyncSession,
    *,
    provider: str,
    model: str,
    task_type: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    latency_ms: int = 0,
    outcome: str = "ok",
    engine: str | None = None,
    task_id: str | None = None,
    cost_usd: float = 0.0,
    tunable_parameter_id: str | None = None,
) -> None:
    session.add(
        LLMCall(
            provider=provider,
            model=model,
            task_type=task_type,
            engine=engine,
            task_id=task_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            latency_ms=latency_ms,
            outcome=outcome,
            cost_usd=cost_usd,
            tunable_parameter_id=tunable_parameter_id,
        )
    )


# --- issues: a finding with an identity, a state, and an earned closure ---------


async def record_issue(
    session: AsyncSession,
    *,
    product_id: str,
    kind: str,
    severity: str,
    title: str,
    location: str = "",
    expected: str = "",
    actual: str = "",
    evidence: str = "",
    found_by: str | None = None,
) -> tuple[Issue, bool]:
    """File a finding, or recognise one already on the books.

    Returns ``(issue, is_new)``. This is the function whose absence let one broken
    checkout be filed four times over 24 days.

    Re-finding an issue does not create a row: it bumps `times_seen` and
    `last_seen_at`, which is how "we have seen this every day for three weeks"
    becomes visible at all. Re-finding a *closed* issue reopens it rather than
    filing a fresh one -- a defect that comes back is a regression, and that is
    worth seeing as one row with a history instead of two rows with none.
    """
    fingerprint = issues.fingerprint_for(
        product_id=product_id, kind=kind, location=location, title=title
    )
    existing = (
        await session.execute(
            select(Issue).where(Issue.product_id == product_id, Issue.fingerprint == fingerprint)
        )
    ).scalar_one_or_none()

    if existing is None:
        # The fingerprint missed. Before filing, check the wording against this
        # product's other live issues -- the reviewer may have named the same
        # defect a different way ("the subscribe button" vs "#subscribe-btn").
        for candidate in await open_issues_for(session, product_id):
            if issues.looks_like_same_issue(
                title, location, candidate.title, candidate.location or ""
            ):
                existing = candidate
                break

    if existing is not None:
        existing.times_seen += 1
        existing.last_seen_at = _now()
        # `fixed` belongs in this list: a defect reported again after someone
        # claimed to repair it means the repair did not work, and leaving it
        # `fixed` would let the next verification pass close it as gone.
        if existing.status in ("closed", "verified", "fixed"):
            existing.status = "open"
            existing.fixed_at = None
            existing.verified_at = None
            existing.closed_at = None
            existing.resolution = None
            existing.fix_task_id = None
            log.info(
                "issue.reopened",
                issue_id=existing.id,
                product_id=product_id,
                times_seen=existing.times_seen,
            )
        await session.flush()
        return existing, False

    issue = Issue(
        product_id=product_id,
        fingerprint=fingerprint,
        kind=kind,
        severity=severity,
        title=title[:500],
        location=location[:500] or None,
        expected=expected or None,
        actual=actual or None,
        evidence=evidence or None,
        status="open",
        found_by=found_by,
        detail={},
    )
    session.add(issue)
    await session.flush()
    log.info("issue.opened", issue_id=issue.id, product_id=product_id, kind=kind, severity=severity)
    return issue, True


async def open_issues_for(session: AsyncSession, product_id: str) -> list[Issue]:
    """Everything still believed present on this product, worst first.

    This is the backlog -- the thing a department manager manages and an employee
    reads before deciding what matters.
    """
    severity_rank = case(
        (Issue.severity == "critical", 0),
        (Issue.severity == "high", 1),
        (Issue.severity == "medium", 2),
        else_=3,
    )
    stmt = (
        select(Issue)
        .where(
            Issue.product_id == product_id,
            Issue.status.in_([*issues.OPEN_STATUSES, *issues.PENDING_VERIFICATION]),
        )
        .order_by(severity_rank, Issue.first_seen_at.asc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def assign_issue_fix(
    session: AsyncSession, issue_id: str, *, task_id: str, assigned_to: str
) -> None:
    """Link an issue to the work commissioned for it. This is the delegation step."""
    await session.execute(
        update(Issue)
        .where(Issue.id == issue_id)
        .values(status="fixing", fix_task_id=task_id, assigned_to=assigned_to)
    )
    log.info("issue.assigned", issue_id=issue_id, task_id=task_id, assigned_to=assigned_to)


async def mark_issue_fixed(session: AsyncSession, *, task_id: str, resolution: str) -> None:
    """A repair finished. That is ``fixed``, deliberately not ``closed``.

    The distinction is the whole point of the table: a patch completing says work
    happened, not that the symptom is gone. Only `verify_unseen_issues` -- a later
    review looking and *not* finding it -- may close an issue.
    """
    await session.execute(
        update(Issue)
        .where(Issue.fix_task_id == task_id, Issue.status == "fixing")
        .values(status="fixed", fixed_at=_now(), resolution=resolution[:1000])
    )


async def verify_unseen_issues(
    session: AsyncSession, product_id: str, *, review_started_at: datetime, reviewed_by: str
) -> int:
    """Close the issues a fresh review looked for and did not find.

    Closure has to be *earned by evidence*, and this is the only place it is. An
    issue marked `fixed` that a subsequent review no longer reports is verified and
    closed; one still being reported stays open however many patches it has had.

    "Did not find" is decided by `last_seen_at` against the moment this review
    started, rather than by the caller passing a list of what it saw. Re-finding an
    issue always bumps `last_seen_at` (see `record_issue`), so the timestamp is the
    same evidence with no bookkeeping to get wrong -- and getting it wrong in the
    optimistic direction would close a defect that is still there, which is the
    single failure this table exists to prevent.

    Scoped to `fixed` only: an `open` issue the reviewer did not mention may simply
    not have been looked at. Only a defect someone claimed to have repaired is a
    candidate for "confirmed gone".
    """
    stmt = select(Issue).where(
        Issue.product_id == product_id,
        Issue.status.in_(issues.PENDING_VERIFICATION),
        Issue.last_seen_at < review_started_at,
    )
    closed = 0
    for issue in (await session.execute(stmt)).scalars().all():
        issue.status = "closed"
        issue.verified_at = _now()
        issue.closed_at = _now()
        issue.detail = {**(issue.detail or {}), "verified_by": reviewed_by}
        closed += 1
        log.info("issue.closed", issue_id=issue.id, product_id=product_id, verified_by=reviewed_by)
    if closed:
        await session.flush()
    return closed


# --- requirements: what work is for, before anyone starts it --------------------


async def record_requirements(
    session: AsyncSession,
    *,
    subject_kind: str,
    subject_id: str,
    requirements: WorkRequirements,
    created_by: str | None = None,
    source_task_id: str | None = None,
) -> str:
    """Store the acceptance criteria for one subject, superseding any earlier set.

    Superseding rather than updating: a build judged against an older standard was
    judged honestly at the time, and rewriting the row in place would make the
    record claim otherwise. It also makes scope change visible as a new row rather
    than an argument nobody wrote down.
    """
    await session.execute(
        update(Requirement)
        .where(
            Requirement.subject_kind == subject_kind,
            Requirement.subject_id == subject_id,
            Requirement.superseded_at.is_(None),
        )
        .values(superseded_at=_now())
    )
    row = Requirement(
        subject_kind=subject_kind,
        subject_id=subject_id,
        goal=requirements.goal,
        must=[c.model_dump() for c in requirements.must],
        should=[c.model_dump() for c in requirements.should],
        out_of_scope=list(requirements.out_of_scope),
        created_by=created_by,
        source_task_id=source_task_id,
    )
    session.add(row)
    await session.flush()
    return row.id


async def live_requirements(
    session: AsyncSession, subject_kind: str, subject_id: str
) -> Requirement | None:
    """The standard this work is currently held to, or None if nobody wrote one."""
    stmt = select(Requirement).where(
        Requirement.subject_kind == subject_kind,
        Requirement.subject_id == subject_id,
        Requirement.superseded_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_build_kind(session: AsyncSession, product_id: str, build_kind: str) -> None:
    """Record whether this product is a validation page or a real MVP.

    Called once, at the point a build of that kind actually starts. `_verify_and_ship`
    reads it rather than being told -- as a parameter it was silently dropped by both
    repair call sites, which is what turned a landing page into a product with a
    broken checkout over 25 days.
    """
    await session.execute(
        update(Product).where(Product.id == product_id).values(build_kind=build_kind)
    )


async def get_build_kind(session: AsyncSession, product_id: str) -> str:
    """Whether this product is still a validation page or a real MVP.

    A single scalar rather than the whole product row, because the verification gate
    needs exactly this one fact and nothing else -- and because fetching the row for
    it couples the gate to every other thing a caller might have stubbed or not.

    Defaults to `validation` for a product that cannot be found: that is where every
    product starts, and erring toward "landing page" means an unknown product is
    never asked for payment wiring it was not meant to have.
    """
    stmt = select(Product.build_kind).where(Product.id == product_id)
    return (await session.execute(stmt)).scalar_one_or_none() or "validation"


@dataclass(slots=True)
class ProductProgress:
    """Whether work on this product is getting anywhere, assembled from rows.

    The continuity a task does not have, and deliberately *not* held in an agent's
    memory. A manager that must carry days of context across a shrinking window is
    blinder than the judges it replaces the moment compression drops something, and
    is trusted more. Compression fails invisibly; a query fails visibly -- you know
    exactly what you asked for.

    Every field here is a count over real rows, so nothing in it can be hallucinated
    and every claim it supports is checkable.
    """

    attempts: int
    blocking_now: int
    blocking_at_last_attempt: int | None
    #: Blocking issues seen on three or more separate passes without ever being
    #: resolved. The sharpest signal there is: not "we have tried a lot" but "we
    #: keep failing at the same thing", which a heal counter could never
    #: distinguish.
    #:
    #: Severity-filtered for the same reason `blocking_now` is. A recurring
    #: *cosmetic* finding is not a wall -- it is usually a deliberate trade, and
    #: reading it as a wall inverts the meaning of the decision that made it.
    #: Observed live: a hardcoded copyright year, medium severity, seen four
    #: times, which the release judgement had twice weighed and explicitly
    #: accepted ("cosmetic, invisible to the demand signal, and cheap to fix
    #: later"). Because `_heal` treats any `stuck` entry as a wall, that accepted
    #: trade escalated the product on its own, and would have gone on doing it
    #: forever. An issue nobody intends to fix recurring is evidence the judgement
    #: is being honoured, not evidence the approach is wrong.
    stuck: list[str]
    #: What was decided last time, so a second look starts from the first rather
    #: than re-deriving it.
    last_decision: str | None

    @property
    def converging(self) -> bool | None:
        """True if the blocking set shrank, False if it grew or held, None when there
        is no signal -- a first attempt, or nothing blocking to converge towards.

        The zero guard is secondary, and was originally mistaken for the whole fix.
        It was added after a build reached `blocking_now = 0` with
        `blocking_at_last_attempt = 0`, the release judgement said **ship**, and
        `0 < 0` evaluated False -- so `_heal` read a wall and escalated a product
        with nothing wrong with it. That was real, but it was one visible face of a
        deeper fault: `blocking_at_last_attempt` was read from the most recent
        `release.decided` event, and `_verify_and_ship` *writes* that event and then
        calls `_heal`, which re-read it. Both numbers came from the same moment, so
        the comparison was against itself and false every time -- the zero case was
        simply where that happened to be visible. Anchoring on the last repair
        attempt (`HealAttempt.blocking_at_attempt`) is the actual fix.

        The guard still earns its place on the path with no issues at all: `_heal`
        also handles deploy failures, which are not issues and never appear in these
        counts, so `blocking_now` is structurally zero there and a count comparison
        means nothing.
        """
        if self.blocking_at_last_attempt is None:
            return None
        if self.blocking_now == 0:
            return None
        return self.blocking_now < self.blocking_at_last_attempt


async def product_progress(session: AsyncSession, product_id: str) -> ProductProgress:
    """The thread across attempts: is the gap list shrinking, and on what are we stuck?

    This replaces `max_heal_cycles` as the thing that decides when to stop. A counter
    cannot tell a run that closed two blockers and opened none at attempt five from
    one that has re-found the same unmet criterion three times running -- the first is
    progress and the second is a wall, and only one of them is worth another attempt.
    """
    open_issues = await open_issues_for(session, product_id)
    blocking = [i for i in open_issues if i.severity in ("critical", "high")]
    blocking_now = len(blocking)
    stuck = [i.title for i in blocking if i.times_seen >= 3]

    product = await get_product(session, product_id)
    attempts = product.heal_attempts if product else 0

    # The blocking figure from the last *repair attempt*, not the last release
    # decision. `_verify_and_ship` writes `release.decided` and then calls `_heal`,
    # which lands here -- so reading that event gave back the number written
    # seconds earlier in the same pass, and `blocking_now < blocking_before`
    # compared the state against itself. False every time, which `_heal` reads as a
    # wall, on the first attempt of every fix_first decision.
    last_attempt = (
        await session.execute(
            select(HealAttempt)
            .where(
                HealAttempt.product_id == product_id,
                HealAttempt.blocking_at_attempt.is_not(None),
            )
            .order_by(HealAttempt.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    # Still read, but only for `last_decision` -- what was decided last time is
    # useful context for the next judgement, and is a different question from how
    # many blockers there were.
    previous = (
        await session.execute(
            select(TaskEvent)
            .where(TaskEvent.product_id == product_id, TaskEvent.event == "release.decided")
            .order_by(TaskEvent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    blocking_before = last_attempt.blocking_at_attempt if last_attempt is not None else None
    last_decision: str | None = None
    if previous is not None and previous.detail:
        last_decision = previous.detail.get("decision")

    return ProductProgress(
        attempts=attempts,
        blocking_now=blocking_now,
        blocking_at_last_attempt=blocking_before,
        stuck=stuck[:8],
        last_decision=last_decision,
    )


async def close_issues_as_invalid(
    session: AsyncSession, *, task_id: str, reason: str
) -> int:
    """Close the issues a task was commissioned for, because the brief was wrong.

    The other half of `RESULT: NOT_NEEDED`. An agent that investigates and finds its
    brief false now ends its own work cleanly -- but the issue that produced the
    brief stayed open, so the next review would commission the same wrong work
    again. Refusing without closing is a loop with extra steps.

    `wont_fix` rather than `closed`: nothing was verified gone, a human decided (via
    an employee's reasoning) that it should not be done. The distinction matters
    because `closed` is reserved for defects a later review confirmed absent.
    """
    stmt = select(Issue).where(Issue.fix_task_id == task_id, Issue.status.in_(("fixing", "open")))
    closed = 0
    for issue in (await session.execute(stmt)).scalars().all():
        issue.status = "wont_fix"
        issue.closed_at = _now()
        issue.resolution = reason[:1000]
        closed += 1
        log.info("issue.closed_as_invalid", issue_id=issue.id, task_id=task_id)
    if closed:
        await session.flush()
    return closed


async def close_resolved_issues(
    session: AsyncSession,
    *,
    product_id: str,
    found_by: str,
    still_present: set[str],
    verified_by: str,
) -> int:
    """Close open issues a source examined exhaustively and did not re-find.

    Only sound for a check that looks at *everything* every run and reports all of
    it: the codegen contract, the SEO check, the requirements comparison. For those,
    "absent from this run's output" really does mean the defect is gone. It is not
    sound for Bug Council, whose silence about a category is not evidence of
    absence -- which is why `verify_unseen_issues` stays narrow and cautious.

    Without this the convergence signal is a lie. An issue filed `open` closes only
    if something marked it `fixed` first, so a defect quietly repaired left its issue
    open forever -- `blocking_now` never falls, every run reads "not converging",
    and the heal loop walls on the second attempt. Observed exactly that way: the
    contract check reported zero violations while its own high-severity issue from
    the previous run was still sitting open.
    """
    stmt = select(Issue).where(
        Issue.product_id == product_id,
        Issue.found_by == found_by,
        Issue.status.in_(("open", "triaged", "fixing")),
    )
    closed = 0
    for issue in (await session.execute(stmt)).scalars().all():
        if issue.fingerprint in still_present:
            continue
        issue.status = "closed"
        issue.verified_at = _now()
        issue.closed_at = _now()
        issue.resolution = f"No longer reported by {found_by}."
        issue.detail = {**(issue.detail or {}), "verified_by": verified_by}
        closed += 1
    if closed:
        await session.flush()
        log.info("issue.closed_resolved", product_id=product_id, source=found_by, closed=closed)
    return closed


@dataclass(slots=True)
class ShippedProductAudit:
    """One shipped product and which of the pipeline's own artifacts it carries.

    Every field is the presence of a row, never a quality judgement. "Did this stage
    run" and "did this stage find anything" are different questions, and only the
    first can be answered without trusting the stage to report on itself.
    """

    id: str
    name: str
    has_requirements: bool
    has_release_decision: bool
    has_preship_review: bool
    has_design_brief: bool


async def recently_shipped_products(
    session: AsyncSession, *, since: datetime
) -> list[ShippedProductAudit]:
    """Products that reached a customer recently, with their pipeline artifacts.

    Feeds `supervisor/audit.py::check_pipeline_completeness`. `VALIDATING` counts as
    shipped: a landing page in front of real visitors is as shipped as anything this
    factory makes, and treating only `LIVE` as shipped is the exact mistake that left
    validation pages unreviewed in the first place.
    """
    stmt = select(Product).where(
        Product.status.in_((ProductStatus.VALIDATING.value, ProductStatus.LIVE.value)),
        Product.updated_at >= since,
    )
    products = list((await session.execute(stmt)).scalars().all())
    if not products:
        return []

    ids = [p.id for p in products]
    with_requirements = set(
        (
            await session.execute(
                select(Requirement.subject_id).where(
                    Requirement.subject_kind == "product", Requirement.subject_id.in_(ids)
                )
            )
        )
        .scalars()
        .all()
    )
    events = (
        await session.execute(
            select(TaskEvent.product_id, TaskEvent.event).where(
                TaskEvent.product_id.in_(ids),
                TaskEvent.event.in_(("release.decided", "preship.reviewed")),
            )
        )
    ).all()
    decided = {pid for pid, event in events if event == "release.decided"}
    reviewed = {pid for pid, event in events if event == "preship.reviewed"}

    return [
        ShippedProductAudit(
            id=p.id,
            name=p.name,
            has_requirements=p.id in with_requirements,
            has_release_decision=p.id in decided,
            has_preship_review=p.id in reviewed,
            has_design_brief=bool(p.design_brief),
        )
        for p in products
    ]


async def record_idea_evidence(
    session: AsyncSession, *, idea_id: str, signals: Sequence[Any]
) -> int:
    """Keep the raw signals an idea was drawn from. Returns how many were stored.

    Called at idea-creation time, where the cluster is still in hand. A few minutes
    later it is gone: `drain_scout_signals` deletes its buffer, and the fixed
    sources never persisted anything at all.

    Idempotent by deletion-and-rewrite rather than upsert -- an idea's evidence is
    the cluster it came from, so a re-run replaces it wholesale rather than merging
    two versions of the same set.
    """
    await session.execute(delete(IdeaEvidence).where(IdeaEvidence.idea_id == idea_id))
    stored = 0
    for signal in signals:
        session.add(
            IdeaEvidence(
                id=new_id(),
                idea_id=idea_id,
                title=str(getattr(signal, "title", ""))[:300],
                body=str(getattr(signal, "body", "")),
                url=str(getattr(signal, "url", ""))[:500],
                source=str(getattr(signal, "source", ""))[:60],
                engagement=int(getattr(signal, "engagement", 0) or 0),
            )
        )
        stored += 1
    if stored:
        await session.flush()
    return stored


async def evidence_for_idea(session: AsyncSession, idea_id: str) -> list[IdeaEvidence]:
    """Everything real people wrote that this idea rests on, most-discussed first."""
    stmt = (
        select(IdeaEvidence)
        .where(IdeaEvidence.idea_id == idea_id)
        .order_by(IdeaEvidence.engagement.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def record_demand_assessment(
    session: AsyncSession, *, idea_id: str, assessment: Any
) -> None:
    """Keep the demand judgement and the audience brief with the idea.

    Stored whatever the verdict, including `thin` -- the reasoning is what lets a
    human disagree with a rejection, and the audience brief is what the landing
    page copy and the acceptance criteria should both be written from. Losing it
    would repeat the mistake that made this necessary: a judgement made, acted on,
    and then unavailable to everything downstream.
    """
    idea = await session.get(Idea, idea_id)
    if idea is None:  # pragma: no cover - caller holds the row
        return
    idea.demand_assessment = assessment.model_dump_json()
    await session.flush()


#: Recipient meaning "nobody in this organisation can answer this" -- the question
#: goes to a person through the existing interrupt path. Not a failure mode: it is
#: what makes the channel useful before any department can answer, because a
#: surfaced question beats a silent retry that burns a repair cycle.
HUMAN = "human"


async def ask_question(
    session: AsyncSession,
    *,
    asked_by_engine: str,
    asked_of: str,
    question: str,
    context: str = "",
    asked_by_task_id: str | None = None,
    blocking_task_id: str | None = None,
    product_id: str | None = None,
) -> Question:
    """Record a question one department cannot answer for itself.

    Does not deliver it -- the caller routes, because routing differs by recipient
    (a department gets a task, a person gets an interrupt) and this layer should not
    know about either.
    """
    row = Question(
        id=new_id(),
        asked_by_engine=asked_by_engine,
        asked_by_task_id=asked_by_task_id,
        blocking_task_id=blocking_task_id,
        product_id=product_id,
        asked_of=asked_of,
        question=question.strip(),
        context=context.strip(),
    )
    session.add(row)
    await session.flush()
    log.info(
        "question.asked",
        question_id=row.id,
        asked_by=asked_by_engine,
        asked_of=asked_of,
        blocking=blocking_task_id,
    )
    return row


async def get_question(session: AsyncSession, question_id: str) -> Question | None:
    return await session.get(Question, question_id)


async def answer_question(
    session: AsyncSession, *, question_id: str, answer: str, answered_by_task_id: str | None = None
) -> str | None:
    """Record an answer and release whatever was waiting on it.

    Returns the task id that was released, or ``None``. Releasing is the half that
    makes this a channel rather than a mailbox: the blocked work was deferred, not
    failed, so answering means clearing `not_before` and letting it run again --
    this time with something it did not have before.
    """
    row = await session.get(Question, question_id)
    if row is None or row.status != "open":
        return None
    row.status = "answered"
    row.answer = answer.strip()
    row.answered_by_task_id = answered_by_task_id
    row.answered_at = _now()

    released: str | None = None
    if row.blocking_task_id:
        task = await session.get(Task, row.blocking_task_id)
        if task is not None and task.status in ("pending", "deferred"):
            task.not_before = _now()
            task.status = "pending"
            released = task.id
    await session.flush()
    log.info("question.answered", question_id=question_id, released_task=released)
    return released


async def open_questions_for(session: AsyncSession, asked_of: str) -> list[Question]:
    """Everything this department has been asked and has not answered."""
    stmt = (
        select(Question)
        .where(Question.asked_of == asked_of, Question.status == "open")
        .order_by(Question.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def answered_questions_for_task(session: AsyncSession, task_id: str) -> list[Question]:
    """Answers this task was waiting on, for the brief when it resumes.

    Without this the loop is pointless: work would be released and then re-run with
    exactly the information that blocked it the first time.
    """
    stmt = (
        select(Question)
        .where(Question.blocking_task_id == task_id, Question.status == "answered")
        .order_by(Question.answered_at)
    )
    return list((await session.execute(stmt)).scalars().all())


@dataclass(slots=True)
class DesignDomain:
    """What the Design manager can see of its own department, as rows not memory.

    Assembled fresh on every tick rather than carried between them. A manager that
    remembers is blinder than the checks it replaces the moment its memory drifts,
    and is trusted more -- every field here is a count or a list over real rows, so
    nothing in it can be hallucinated and every claim it supports is checkable.
    """

    #: Shipped, and nobody has written down how it should look. The most common
    #: reason there is work to do.
    without_guidelines: list[tuple[str, str]]
    #: Live, with guidelines, and no design review recorded against the current
    #: deployment. The manager decides whether that is worth its cost.
    unreviewed: list[tuple[str, str]]
    #: Design findings still open across every product, newest first. Several
    #: products sharing one complaint is evidence about the house style rather than
    #: about any of them.
    open_findings: list[tuple[str, str]]
    #: Questions addressed to Design and not yet answered.
    open_questions: int


async def design_domain(session: AsyncSession) -> DesignDomain:
    """Everything the Design manager needs to decide what to do next."""
    live = (
        (
            await session.execute(
                select(Product).where(
                    Product.status.in_(
                        (ProductStatus.VALIDATING.value, ProductStatus.LIVE.value)
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    ids = [p.id for p in live]

    with_guidelines: set[str] = set()
    reviewed: set[str] = set()
    if ids:
        with_guidelines = set(
            (
                await session.execute(
                    select(Requirement.subject_id).where(
                        Requirement.subject_kind == "design",
                        Requirement.subject_id.in_(ids),
                        Requirement.superseded_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        reviewed = {
            pid
            for pid in (
                await session.execute(
                    select(TaskEvent.product_id).where(
                        TaskEvent.product_id.in_(ids),
                        TaskEvent.event == "design.reviewed",
                    )
                )
            )
            .scalars()
            .all()
            if pid is not None
        }

    findings = (
        (
            await session.execute(
                select(Issue)
                .where(
                    Issue.found_by.like("design%"),
                    Issue.status.in_(issues.OPEN_STATUSES),
                )
                .order_by(Issue.last_seen_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )

    pending = await open_questions_for(session, EngineName.DESIGN.value)

    return DesignDomain(
        without_guidelines=[(p.id, p.name) for p in live if p.id not in with_guidelines],
        unreviewed=[
            (p.id, p.name) for p in live if p.id in with_guidelines and p.id not in reviewed
        ],
        open_findings=[(i.product_id or "", i.title) for i in findings],
        open_questions=len(pending),
    )


async def record_design_guidelines(
    session: AsyncSession,
    *,
    product_id: str,
    guidelines: Any,
    source_task_id: str | None = None,
) -> str:
    """Store how one product should look, superseding whatever came before.

    Kept in `requirements` under `subject_kind='design'` rather than a table of its
    own. They are the same kind of thing -- a standard written before the work,
    that the builder works to and a reviewer judges against -- and they need the
    same property that matters most: superseded rather than rewritten, so a
    regeneration is held to the same bar as the build it replaces and the previous
    version survives as a record of what was decided before.
    """
    await session.execute(
        update(Requirement)
        .where(
            Requirement.subject_kind == "design",
            Requirement.subject_id == product_id,
            Requirement.superseded_at.is_(None),
        )
        .values(superseded_at=_now())
    )
    row = Requirement(
        id=new_id(),
        subject_kind="design",
        subject_id=product_id,
        goal=guidelines.direction,
        # The schema's own shape, carried through the generic columns: `must_hold`
        # is what a reviewer may fail the page on, `must_avoid` is what must not
        # appear, and the palette and typography ride along as context for both.
        must=[{"criterion": item, "why": "design guideline"} for item in guidelines.must_hold],
        should=[
            {"criterion": f"Palette: {item}", "why": "stated before building"}
            for item in guidelines.palette
        ]
        + [{"criterion": f"Typography: {guidelines.typography}", "why": "stated before building"}],
        out_of_scope=list(guidelines.must_avoid),
        created_by="design:guidelines",
        source_task_id=source_task_id,
    )
    session.add(row)
    await session.flush()
    log.info("design.guidelines_recorded", product_id=product_id, id=row.id)
    return row.id


async def live_design_guidelines(session: AsyncSession, product_id: str) -> Any:
    """The current design guidelines for a product, or ``None``.

    Returns a `DesignGuidelines`, rebuilt from the generic columns it was stored in.
    """
    from factory.state.schemas import DesignGuidelines

    row = (
        await session.execute(
            select(Requirement).where(
                Requirement.subject_kind == "design",
                Requirement.subject_id == product_id,
                Requirement.superseded_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    palette = [
        c["criterion"].removeprefix("Palette: ")
        for c in (row.should or [])
        if str(c.get("criterion", "")).startswith("Palette: ")
    ]
    typography = next(
        (
            str(c["criterion"]).removeprefix("Typography: ")
            for c in (row.should or [])
            if str(c.get("criterion", "")).startswith("Typography: ")
        ),
        "",
    )
    try:
        return DesignGuidelines(
            direction=row.goal,
            palette=palette or ["(none recorded)", "(none recorded)"],
            typography=typography or "(none recorded)",
            must_hold=[str(c.get("criterion", "")) for c in (row.must or [])] or ["(none)"],
            must_avoid=list(row.out_of_scope or []),
        )
    except ValueError:
        # A stored row that no longer satisfies the schema is a fact worth seeing
        # rather than a crash: the caller falls back to the house style.
        log.warning("design.guidelines_corrupt", product_id=product_id, id=row.id)
        return None
