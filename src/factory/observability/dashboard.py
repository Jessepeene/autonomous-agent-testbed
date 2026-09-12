"""Read-only aggregate for the operator dashboard's `GET /dashboard/state`.

Same shape as `observability/metrics.py::collect` -- one function that
assembles everything a caller needs from data that already exists, adding new
row-level repository queries only where no listing existed before. Every
field here reads real columns; nothing is fabricated to fill a gap the
current schema can't back (see the dashboard-integration plan for the
per-field reasoning on what got dropped rather than invented).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.development.design_brief import DesignBrief
from factory.learning import stats as learning_stats
from factory.learning import store as learning_store
from factory.observability import metrics as metrics_module
from factory.observability.costs import provider_outcome_rates, total_usd
from factory.preflight import PreflightReport
from factory.settings import EngineName, Settings
from factory.state import repository


@dataclass(slots=True)
class ProviderHealthSection:
    status: str  # "up" | "degraded" | "down"
    errors_30m: int
    calls_30m: int


@dataclass(slots=True)
class HealthSection:
    mode: str  # "dry_run" | "active"
    db_connected: bool
    provider_health: dict[str, ProviderHealthSection]
    global_pause: bool
    sandbox_health: str  # "clean" | "degraded"
    daily_llm_spend_cap_usd: float | None


@dataclass(slots=True)
class EngineSection:
    name: str
    status: str  # "idle" | "running" | "paused"
    in_flight: int
    max_concurrency: int | None  # None = no configured ceiling
    disabled_reason: str | None


@dataclass(slots=True)
class TaskSection:
    id: str
    kind: str
    status: str
    engine: str | None
    priority: int
    attempt_count: int
    idempotency_key: str
    updated_at: datetime


@dataclass(slots=True)
class ProductSection:
    id: str
    name: str
    niche: str
    idea_score: float
    status: str
    heal_attempts: int
    mrr_cents: int
    updated_at: datetime


@dataclass(slots=True)
class BusinessPlanSection:
    """Everything the dashboard's product detail view needs to show a
    "business plan" -- all of it already lives on `Idea` (joined via
    `Product.idea_id`), so this is a pure read, no new generation."""

    problem_statement: str
    target_niche: str
    monetization_hypothesis: str
    self_serve_price_usd: float
    score: float
    evidence_count: int
    source_url: str
    pain_severity: float | None
    willingness_to_pay: float | None
    build_feasibility: float | None
    adoption_ease: float | None
    defensibility: float | None


@dataclass(slots=True)
class DesignBriefSection:
    palette: list[str]
    typography: str
    tone: str


@dataclass(slots=True)
class ProductDetail:
    id: str
    name: str
    status: str
    deploy_url: str | None
    heal_attempts: int
    created_at: datetime
    updated_at: datetime
    business_plan: BusinessPlanSection
    #: None if never generated -- either still building, or an already-live
    #: product that predates this feature and hasn't been backfilled yet
    #: (see `api/dashboard.py`'s generate-design-brief endpoint).
    design_brief: DesignBriefSection | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class InterruptSection:
    id: str
    reason: str
    subject: str
    detail: str
    headline: str | None
    reply_hint: str | None
    product_id: str | None
    created_at: datetime


@dataclass(slots=True)
class TrialSection:
    id: str
    key: str
    kind: str
    status: str
    target_metric: str
    numerator: int
    denominator: int
    baseline_numerator: int | None
    baseline_denominator: int | None
    p_value: float | None
    created_at: datetime


@dataclass(slots=True)
class ActivityItem:
    id: str
    level: str  # "info" | "warn"
    source: str  # "task" | "interrupt" | "ledger"
    message: str
    created_at: datetime


@dataclass(slots=True)
class MarketingSection:
    leads_total: int
    outreach_sent: int
    outreach_suppressed: int
    deliverability_sent_30d: int
    deliverability_complained_30d: int
    deliverability_bounced_30d: int
    freeze_active: bool
    freeze_reason: str | None
    spam_complaint_rate_threshold: float
    bounce_rate_threshold: float


@dataclass(slots=True)
class DashboardSnapshot:
    health: HealthSection
    metrics: dict[str, object]  # FactoryMetrics.as_dict() -- reused wholesale
    marketing: MarketingSection
    engines: list[EngineSection]
    tasks: list[TaskSection]
    products: list[ProductSection]
    interrupts: list[InterruptSection]
    trials: list[TrialSection]
    activity: list[ActivityItem]
    balance_cents: int
    reinvest_pool_cents: int
    last_ledger_write: datetime | None
    max_task_attempts: int
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _provider_status(errors: int, total: int, *, configured: bool) -> str:
    if not configured:
        return "down"
    if total == 0:
        return "up"  # no recent evidence of trouble
    rate = errors / total
    if rate < 0.10:
        return "up"
    if rate < 0.50 or total < 3:
        return "degraded"
    return "down"


async def _health(
    session: AsyncSession, *, settings: Settings, report: PreflightReport
) -> HealthSection:
    rates = await provider_outcome_rates(session, minutes=30)
    provider_health = {}
    for provider_name, configured in (
        ("anthropic", settings.has_anthropic),
        ("gemini", settings.has_gemini),
    ):
        errors, total = rates.get(provider_name, (0, 0))
        provider_health[provider_name] = ProviderHealthSection(
            status=_provider_status(errors, total, configured=configured),
            errors_30m=errors,
            calls_30m=total,
        )

    db_connected = True
    try:
        await repository.count_pending_tasks(session)
    except Exception:  # liveness probe -- any failure here means "not connected"
        db_connected = False

    global_pause = False
    if settings.daily_llm_spend_cap_usd is not None:
        spent = await total_usd(session, hours=24)
        global_pause = spent >= settings.daily_llm_spend_cap_usd

    return HealthSection(
        mode="dry_run" if settings.factory_dry_run else "active",
        db_connected=db_connected,
        provider_health=provider_health,
        global_pause=global_pause,
        sandbox_health="degraded" if EngineName.DEVELOPMENT in report.disabled_engines else "clean",
        daily_llm_spend_cap_usd=settings.daily_llm_spend_cap_usd,
    )


async def _engines(
    session: AsyncSession, *, settings: Settings, report: PreflightReport
) -> list[EngineSection]:
    in_flight = await repository.tasks_in_flight_by_engine(session)
    sections = []
    for engine in EngineName:
        disabled_reason = report.disabled_engines.get(engine)
        count = in_flight.get(engine.value, 0)
        status = "paused" if disabled_reason else ("running" if count > 0 else "idle")
        sections.append(
            EngineSection(
                name=engine.value,
                status=status,
                in_flight=count,
                max_concurrency=(
                    settings.max_concurrent_builds if engine is EngineName.DEVELOPMENT else None
                ),
                disabled_reason=disabled_reason,
            )
        )
    return sections


async def _tasks(session: AsyncSession, *, limit: int) -> list[TaskSection]:
    return [
        TaskSection(
            id=t.id,
            kind=t.kind,
            status=t.status,
            engine=t.engine,
            priority=t.priority,
            attempt_count=t.attempt_count,
            idempotency_key=t.idempotency_key,
            updated_at=t.updated_at,
        )
        for t in await repository.recent_tasks(session, limit=limit)
    ]


async def _products(session: AsyncSession, *, limit: int) -> list[ProductSection]:
    pairs = await repository.products_with_idea(session, limit=limit)
    mrr = await repository.mrr_by_product(session)
    return [
        ProductSection(
            id=product.id,
            name=product.name,
            niche=idea.target_niche,
            idea_score=idea.score,
            status=product.status,
            heal_attempts=product.heal_attempts,
            mrr_cents=mrr.get(product.id, 0),
            updated_at=product.updated_at,
        )
        for product, idea in pairs
    ]


async def collect_product_detail(session: AsyncSession, product_id: str) -> ProductDetail | None:
    """The dashboard's product detail view -- unlike `collect()`, this isn't
    part of the bulk snapshot poll; it's fetched on demand when an operator
    opens one product's card."""
    pair = await repository.get_product_with_idea(session, product_id)
    if pair is None:
        return None
    product, idea = pair

    design: DesignBriefSection | None = None
    if product.design_brief:
        try:
            brief = DesignBrief.model_validate_json(product.design_brief)
        except ValueError:
            brief = None
        if brief is not None:
            design = DesignBriefSection(
                palette=brief.palette, typography=brief.typography, tone=brief.tone
            )

    return ProductDetail(
        id=product.id,
        name=product.name,
        status=product.status,
        deploy_url=product.deploy_url,
        heal_attempts=product.heal_attempts,
        created_at=product.created_at,
        updated_at=product.updated_at,
        business_plan=BusinessPlanSection(
            problem_statement=idea.problem_statement,
            target_niche=idea.target_niche,
            monetization_hypothesis=idea.monetization_hypothesis,
            self_serve_price_usd=idea.self_serve_price_usd,
            score=idea.score,
            evidence_count=idea.evidence_count,
            source_url=idea.source_url,
            pain_severity=idea.pain_severity,
            willingness_to_pay=idea.willingness_to_pay,
            build_feasibility=idea.build_feasibility,
            adoption_ease=idea.adoption_ease,
            defensibility=idea.defensibility,
        ),
        design_brief=design,
    )


async def _interrupts(session: AsyncSession, *, limit: int) -> list[InterruptSection]:
    return [
        InterruptSection(
            id=i.id,
            reason=i.reason,
            subject=i.subject,
            detail=i.detail,
            headline=i.headline,
            reply_hint=i.reply_hint,
            product_id=i.product_id,
            created_at=i.created_at,
        )
        for i in await repository.list_open_interrupts(session, limit=limit)
    ]


async def _trials(session: AsyncSession, *, limit: int) -> list[TrialSection]:
    rows = await learning_store.list_all(session, limit=limit)
    sections = []
    for row in rows:
        successes, total = await learning_store.episode_counts(session, row.id)
        base_successes: int | None = None
        base_total: int | None = None
        p_value: float | None = None
        if row.parent_id is not None:
            base_successes, base_total = await learning_store.episode_counts(
                session, row.parent_id, limit=200
            )
            if total > 0 and base_total > 0:
                p_value = learning_stats.fisher_exact_less(
                    successes, total - successes, base_successes, base_total - base_successes
                )
        sections.append(
            TrialSection(
                id=row.id,
                key=row.key,
                kind=row.kind,
                status=row.status,
                target_metric=row.target_metric,
                numerator=successes,
                denominator=total,
                baseline_numerator=base_successes,
                baseline_denominator=base_total,
                p_value=p_value,
                created_at=row.created_at,
            )
        )
    return sections


async def _marketing(
    session: AsyncSession, fm: metrics_module.FactoryMetrics, *, settings: Settings
) -> MarketingSection:
    sent, complained, bounced = await repository.deliverability_counts(session, window_days=30)
    freeze = await repository.active_marketing_freeze(session)
    return MarketingSection(
        leads_total=fm.leads_total,
        outreach_sent=fm.outreach_sent,
        outreach_suppressed=fm.outreach_suppressed,
        deliverability_sent_30d=sent,
        deliverability_complained_30d=complained,
        deliverability_bounced_30d=bounced,
        freeze_active=freeze is not None,
        freeze_reason=freeze.reason if freeze is not None else None,
        spam_complaint_rate_threshold=settings.spam_complaint_rate_threshold,
        bounce_rate_threshold=settings.bounce_rate_threshold,
    )


async def _activity(
    tasks: list[TaskSection], interrupts: list[InterruptSection], *, limit: int
) -> list[ActivityItem]:
    """Newest-first union of real rows already fetched above -- deliberately
    not a log tail (no log table exists anywhere in this codebase; see the
    dashboard plan for why this stays DB-synthesized rather than reaching
    through the Docker socket for real log lines)."""
    items = [
        ActivityItem(
            id=t.id,
            level="warn" if t.status == "failed" else "info",
            source="task",
            message=f"{t.kind} ({t.status}) on {t.engine or 'unassigned'}",
            created_at=t.updated_at,
        )
        for t in tasks
    ] + [
        ActivityItem(
            id=i.id,
            level="warn",
            source="interrupt",
            message=i.headline or i.subject,
            created_at=i.created_at,
        )
        for i in interrupts
    ]
    items.sort(key=lambda item: item.created_at, reverse=True)
    return items[:limit]


async def collect(
    session: AsyncSession,
    *,
    settings: Settings,
    report: PreflightReport,
    row_limit: int = 50,
) -> DashboardSnapshot:
    fm = await metrics_module.collect(session)
    tasks = await _tasks(session, limit=row_limit)
    interrupts = await _interrupts(session, limit=row_limit)
    balance_cents, reinvest_pool_cents = await repository.current_balance(session)
    last_entry = await repository.latest_ledger_entry(session)

    return DashboardSnapshot(
        health=await _health(session, settings=settings, report=report),
        metrics=fm.as_dict(),
        marketing=await _marketing(session, fm, settings=settings),
        engines=await _engines(session, settings=settings, report=report),
        tasks=tasks,
        products=await _products(session, limit=row_limit),
        interrupts=interrupts,
        trials=await _trials(session, limit=row_limit),
        activity=await _activity(tasks, interrupts, limit=row_limit),
        balance_cents=balance_cents,
        reinvest_pool_cents=reinvest_pool_cents,
        last_ledger_write=last_entry.created_at if last_entry is not None else None,
        max_task_attempts=settings.max_task_attempts,
    )
