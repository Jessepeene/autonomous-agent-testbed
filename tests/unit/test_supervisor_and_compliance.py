"""Discovery throttle, the LIA contact gate, and preflight degradation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from factory.engines.compliance import assessor, lia
from factory.hitl import interrupts
from factory.llm.providers.fake import FakeProvider
from factory.preflight import (
    Finding,
    Severity,
    check_sandbox_workspace_sharing,
    run_preflight,
    write_missing_credentials,
)
from factory.settings import EngineName, Settings
from factory.state import repository
from factory.state.schemas import (
    ComplianceAssessment,
    ComplianceStatus,
    InterruptReason,
    LeadRecord,
    LIAVerdict,
    NextAction,
    ValidatedIdea,
)
from factory.supervisor import orchestrator
from factory.supervisor.context import FactoryContext


def _snapshot(**overrides: Any) -> orchestrator.FactorySnapshot:
    base: dict[str, Any] = {
        "eligible_engines": ["development", "discovery"],
        "disabled_engines": {},
        "pending_tasks_total": 5,
        "pending_by_engine": {"discovery": 5},
        "active_dev_products": 0,
        "max_concurrent_builds": 2,
        "pending_dev_retries": 0,
        "approved_ideas_waiting": 3,
        "pending_ideas": 10,
        "open_interrupts": 0,
        "spend_limit_cents": 0,
        "dry_run": True,
        "churning_products": [],
    }
    base.update(overrides)
    return orchestrator.FactorySnapshot(**base)


# --- Discovery throttle -----------------------------------------------------


async def test_orchestrator_holds_discovery_at_capacity(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At MAX_CONCURRENT_BUILDS, ideas wait instead of flooding the queue."""

    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(active_dev_products=2, max_concurrent_builds=2)

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert decision.next_action is NextAction.HOLD_DISCOVERY
    assert decision.target_engine is EngineName.SUPERVISOR
    assert "capacity" in decision.rationale


async def test_orchestrator_does_not_hold_below_capacity(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(active_dev_products=1, max_concurrent_builds=2, pending_tasks_total=0)

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    # Nothing queued, so the tick does maintenance rather than sleeping.
    assert decision.next_action is NextAction.IDLE_BACKLOG


async def test_no_ready_work_still_does_backlog(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(pending_tasks_total=0)

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]
    assert decision.next_action is NextAction.IDLE_BACKLOG


def test_fallback_prefers_revenue_bearing_engines() -> None:
    decision = orchestrator._fallback(
        _snapshot(pending_by_engine={"discovery": 3, "finance": 1, "marketing": 2})
    )
    assert decision.target_engine is EngineName.FINANCE


def test_capacity_property_is_inclusive() -> None:
    assert _snapshot(active_dev_products=2, max_concurrent_builds=2).dev_at_capacity is True
    assert _snapshot(active_dev_products=1, max_concurrent_builds=2).dev_at_capacity is False


async def test_dev_at_capacity_routes_to_marketing_when_it_has_queued_work(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Distribution outranks starting another build: a capacity tick with organic
    outreach waiting must not fall through to generic backlog maintenance."""

    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(
            active_dev_products=2,
            max_concurrent_builds=2,
            pending_by_engine={"discovery": 1, "marketing": 3},
        )

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert decision.next_action is NextAction.ROUTE
    assert decision.target_engine is EngineName.MARKETING


async def test_dev_at_capacity_with_no_marketing_work_still_holds_discovery(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(
            active_dev_products=2, max_concurrent_builds=2, pending_by_engine={"discovery": 1}
        )

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert decision.next_action is NextAction.HOLD_DISCOVERY


async def test_a_pending_patch_task_routes_to_development_even_at_capacity(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for a real deadlock: a product stuck in PATCHING is already
    counted in active_dev_products, so claiming its own patch task cannot admit a
    new product into the pipeline. Blocking it anyway (the old behavior) meant the
    product could never leave PATCHING, which kept dev_at_capacity true forever --
    observed live as HOLD_DISCOVERY repeating every tick with the patch task sitting
    pending indefinitely."""

    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(
            active_dev_products=2,
            max_concurrent_builds=2,
            pending_by_engine={"development": 1},
            pending_dev_retries=1,
        )

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert decision.next_action is NextAction.ROUTE
    assert decision.target_engine is EngineName.DEVELOPMENT


async def test_a_pending_patch_task_outranks_marketing_at_capacity(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixing a live product outranks starting one *and* outranks selling one --
    the patch check runs before the marketing check for exactly this reason."""

    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(
            active_dev_products=2,
            max_concurrent_builds=2,
            pending_by_engine={"development": 1, "marketing": 3},
            pending_dev_retries=1,
        )

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert decision.next_action is NextAction.ROUTE
    assert decision.target_engine is EngineName.DEVELOPMENT


async def test_a_retried_build_mvp_for_a_stuck_product_also_routes_at_capacity(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the same deadlock, one task kind over: a BUILD_MVP that
    threw partway through leaves its product stuck in BUILDING (never reset on
    failure), and the old check only ever recognised PATCH_PRODUCT as a safe
    retry -- a retried BUILD_MVP for that same already-counted product was
    invisible to it, so two such products could permanently fill both capacity
    slots with retries the gate would never claim."""

    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(
            active_dev_products=2,
            max_concurrent_builds=2,
            pending_by_engine={"development": 1},
            pending_dev_retries=1,
        )

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert decision.next_action is NextAction.ROUTE
    assert decision.target_engine is EngineName.DEVELOPMENT


# --- financial circuit breaker ------------------------------------------------


async def test_budget_check_is_a_noop_when_the_cap_is_unset(ctx: FactoryContext) -> None:
    ctx.settings.daily_llm_spend_cap_usd = None
    assert await orchestrator._check_budget(None, ctx) is None  # type: ignore[arg-type]


async def test_budget_check_is_a_noop_below_the_cap(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.daily_llm_spend_cap_usd = 10.0

    async def fake_total_usd(session: Any, *, hours: int = 24) -> float:
        return 5.0

    monkeypatch.setattr(orchestrator.costs, "total_usd", fake_total_usd)
    assert await orchestrator._check_budget(None, ctx) is None  # type: ignore[arg-type]


async def test_budget_check_trips_at_or_above_the_cap(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.settings.daily_llm_spend_cap_usd = 10.0
    raised: dict[str, Any] = {}

    async def fake_total_usd(session: Any, *, hours: int = 24) -> float:
        return 10.0

    async def fake_request(session: Any, **kwargs: Any) -> str:
        raised.update(kwargs)
        return "interrupt-1"

    monkeypatch.setattr(orchestrator.costs, "total_usd", fake_total_usd)
    monkeypatch.setattr(orchestrator.interrupts, "request", fake_request)

    decision = await orchestrator._check_budget(None, ctx)  # type: ignore[arg-type]

    assert decision is not None
    assert decision.next_action is NextAction.BUDGET_PAUSED
    assert decision.target_engine is EngineName.SUPERVISOR
    assert raised["reason"] is InterruptReason.BUDGET_EXCEEDED


async def test_tripped_budget_overrides_dev_capacity_and_everything_else(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The breaker is checked before `build_snapshot` is even called — a tripped
    breaker must win regardless of what the rest of the snapshot would otherwise
    decide."""
    ctx.settings.daily_llm_spend_cap_usd = 1.0

    async def fake_total_usd(session: Any, *, hours: int = 24) -> float:
        return 999.0

    async def fake_request(session: Any, **kwargs: Any) -> str:
        return "interrupt-1"

    async def exploding_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        raise AssertionError("build_snapshot must not be called once the breaker trips")

    monkeypatch.setattr(orchestrator.costs, "total_usd", fake_total_usd)
    monkeypatch.setattr(orchestrator.interrupts, "request", fake_request)
    monkeypatch.setattr(orchestrator, "build_snapshot", exploding_snapshot)

    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]
    assert decision.next_action is NextAction.BUDGET_PAUSED


# --- orchestrator prompt rendering ------------------------------------------


def test_system_prompt_renders_the_role_catalogue_not_a_literal_placeholder() -> None:
    """Regression: the template's ``{roles}`` slot was once left unformatted, so the
    literal string "{roles}" was sent to the model instead of the actual catalogue."""
    from factory.agents.roles import PIPELINE_SCOPED_ROLES, WORKSPACE_SCOPED_ROLES, RoleName

    rendered = orchestrator._system_prompt()
    assert "{roles}" not in rendered
    for name in RoleName:
        # Workspace-scoped roles are deliberately absent -- the Orchestrator must not
        # be offered a role that is only safe inside development/graph.py's _patch.
        if name in WORKSPACE_SCOPED_ROLES:
            assert name.value not in rendered
            continue
        # Pipeline-scoped roles are the same idea for a different caller -- see
        # DISCOVERY_SCOUT, only ever spawned from engines/discovery/scout.py.
        if name in PIPELINE_SCOPED_ROLES:
            assert name.value not in rendered
            continue
        assert name.value in rendered


async def test_spawn_agent_decision_passes_through_when_target_is_eligible(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SUPERVISOR is always an eligible target, so a well-formed SPAWN_AGENT decision
    from the model must reach the graph unchanged, not be rewritten to a fallback."""
    from factory.state.schemas import OrchestratorDecision

    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot()

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    gemini_fake.register(
        OrchestratorDecision,
        OrchestratorDecision(
            next_action=NextAction.SPAWN_AGENT,
            target_engine=EngineName.SUPERVISOR,
            agent_role="researcher",
            agent_objective="find out whether a competitor shipped this already",
            rationale="No fixed engine answers open market questions.",
        ),
    )

    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert decision.next_action is NextAction.SPAWN_AGENT
    assert decision.agent_role == "researcher"


async def test_a_model_route_to_an_engine_with_zero_pending_tasks_falls_back_to_deterministic(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found live: `approved_ideas_waiting` can stay non-zero for a product that has
    already finished its landing-page build and is just sitting in VALIDATING,
    waiting on signals (a deliberate design choice -- see
    test_orchestrator_snapshot.py). In production this convinced the model
    Development still had unbuilt work and it routed there, tick after tick for
    five minutes straight, even though `pending_by_engine['development']` was
    provably 0 -- nothing was ever actually claimed, and real queued work sat
    untouched on Marketing. Routing to Compliance here instead of Development:
    the default `ctx` fixture disables Development entirely, and a target the
    model is not even eligible for would be caught by the pre-existing
    eligibility check regardless of this guard -- Compliance is eligible but,
    per this snapshot, has nothing queued, which is what actually isolates the
    new check. Emptiness is a fact the model cannot be right or wrong about, so
    a ROUTE to a zero-pending-tasks engine must never be trusted as-is."""
    from factory.state.schemas import OrchestratorDecision

    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(pending_by_engine={"marketing": 1}, approved_ideas_waiting=1)

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    gemini_fake.register(
        OrchestratorDecision,
        OrchestratorDecision(
            next_action=NextAction.ROUTE,
            target_engine=EngineName.COMPLIANCE,
            rationale="An idea is waiting on compliance review.",
        ),
    )

    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert decision.next_action is NextAction.ROUTE
    assert decision.target_engine is EngineName.MARKETING


async def test_a_model_route_to_an_engine_with_real_pending_work_is_trusted(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard above must not reject every model decision -- only ones that
    provably cannot claim anything. Discovery has real pending work here too, so a
    model preferring it over the deterministic priority order's own top pick is a
    legitimate judgement call, not a factual error, and must pass through."""
    from factory.state.schemas import OrchestratorDecision

    async def fake_snapshot(session: Any, context: Any) -> orchestrator.FactorySnapshot:
        return _snapshot(pending_by_engine={"marketing": 1, "discovery": 2})

    monkeypatch.setattr(orchestrator, "build_snapshot", fake_snapshot)
    gemini_fake.register(
        OrchestratorDecision,
        OrchestratorDecision(
            next_action=NextAction.ROUTE,
            target_engine=EngineName.DISCOVERY,
            rationale="Discovery work is fresher and cheaper to clear first.",
        ),
    )

    decision = await orchestrator.decide(None, ctx)  # type: ignore[arg-type]

    assert decision.next_action is NextAction.ROUTE
    assert decision.target_engine is EngineName.DISCOVERY


# --- LIA contact gate -------------------------------------------------------


def _verdict(passed: bool, *, reviewer_approve: bool = True) -> LIAVerdict:
    text = "a" * 30
    return LIAVerdict(
        role_utility_link=text,
        necessity=text,
        balancing_test=text,
        passed=passed,
        would_a_careful_reviewer_approve=reviewer_approve,
    )


def _assessment(
    status: ComplianceStatus, confidence: float, passed: bool, *, reviewer_approve: bool = True
) -> ComplianceAssessment:
    return ComplianceAssessment(
        status=status,
        justification="j" * 30,
        confidence=confidence,
        lia_verdict=_verdict(passed, reviewer_approve=reviewer_approve),
    )


@pytest.fixture(autouse=True)
def _no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the two repository calls the LIA path makes, so no database is needed."""

    async def zero(*args: Any, **kwargs: Any) -> int:
        return 0

    async def noop(*args: Any, **kwargs: Any) -> str | None:
        return "int_test"

    monkeypatch.setattr(repository, "outreach_count_for_domain", zero)
    monkeypatch.setattr(interrupts, "request", noop)


async def test_rule_block_short_circuits_before_any_model_call(
    ctx: FactoryContext, lead: LeadRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A personal mailbox is refused without spending a token on an assessment."""
    called = False

    async def spy(*args: Any, **kwargs: Any) -> ComplianceAssessment:
        nonlocal called
        called = True
        return _assessment(ComplianceStatus.APPROVED, 0.99, True)

    monkeypatch.setattr(assessor, "assess_lead", spy)
    personal = lead.model_copy(update={"work_email": "ada@gmail.com"})

    decision = await lia.evaluate_lead(ctx, None, personal, product_summary="tool")  # type: ignore[arg-type]

    assert decision.allowed is False
    assert called is False, "the model was consulted about a lead the rules already refused"


async def test_failed_lia_blocks_contact(
    ctx: FactoryContext, lead: LeadRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def rejected(*args: Any, **kwargs: Any) -> ComplianceAssessment:
        return _assessment(ComplianceStatus.REJECTED, 0.95, False)

    monkeypatch.setattr(assessor, "assess_lead", rejected)
    decision = await lia.evaluate_lead(ctx, None, lead, product_summary="tool")  # type: ignore[arg-type]
    assert decision.allowed is False


async def test_flagged_lia_blocks_contact_and_escalates(
    ctx: FactoryContext, lead: LeadRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def flagged(*args: Any, **kwargs: Any) -> ComplianceAssessment:
        return _assessment(ComplianceStatus.FLAG_FOR_REVIEW, 0.5, True)

    monkeypatch.setattr(assessor, "assess_lead", flagged)
    decision = await lia.evaluate_lead(ctx, None, lead, product_summary="tool")  # type: ignore[arg-type]
    assert decision.allowed is False
    assert "review" in decision.reason


async def test_approved_lia_permits_contact(
    ctx: FactoryContext, lead: LeadRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def approved(*args: Any, **kwargs: Any) -> ComplianceAssessment:
        return _assessment(ComplianceStatus.APPROVED, 0.95, True)

    monkeypatch.setattr(assessor, "assess_lead", approved)
    decision = await lia.evaluate_lead(ctx, None, lead, product_summary="tool")  # type: ignore[arg-type]
    assert decision.allowed is True


def test_low_confidence_is_downgraded_not_approved(settings: Settings) -> None:
    """An assessor that is unsure has not established a lawful basis."""
    assert settings.compliance_min_confidence == 0.85
    downgraded = _assessment(ComplianceStatus.APPROVED, 0.5, True).model_copy(
        update={"status": ComplianceStatus.FLAG_FOR_REVIEW}
    )
    assert downgraded.approved is False


def test_refusal_helper_never_approves() -> None:
    refusal = assessor._refuse("provider outage")
    assert refusal.approved is False
    assert refusal.confidence == 0.0
    assert refusal.lia_verdict is not None
    assert refusal.lia_verdict.passed is False
    assert refusal.lia_verdict.would_a_careful_reviewer_approve is False


def test_a_failed_reviewer_signoff_flags_for_review_even_with_every_limb_passing(
    settings: Settings,
) -> None:
    """Tier 0: a justification can correctly reference the real facts (every
    limb passes) while still not being a decision a careful reviewer would
    stand behind. This is a softer signal than a failed limb -- same failure
    mode as low confidence (escalate to a human), not an automatic REJECTED."""
    high_confidence_but_flagged = _assessment(
        ComplianceStatus.APPROVED, 0.99, True, reviewer_approve=False
    )

    result = assessor.finalise_lead_assessment(high_confidence_but_flagged, settings=settings)

    assert result.status is ComplianceStatus.FLAG_FOR_REVIEW
    assert result.approved is False


def test_a_passed_reviewer_signoff_does_not_block_an_otherwise_approved_verdict(
    settings: Settings,
) -> None:
    high_confidence_and_signed_off = _assessment(
        ComplianceStatus.APPROVED, 0.99, True, reviewer_approve=True
    )

    result = assessor.finalise_lead_assessment(high_confidence_and_signed_off, settings=settings)

    assert result.status is ComplianceStatus.APPROVED


# --- idea-level compliance thresholding --------------------------------------

_IDEA = ValidatedIdea(
    id="idea-1",
    problem_statement="Bookkeepers reconcile invoices by hand every week.",
    target_niche="bookkeeping",
    source_url="https://example.test/thread",
    monetization_hypothesis="Bookkeepers pay EUR 49/month per seat.",
    score=0.8,
    evidence_count=3,
    created_at=datetime.now(UTC),
)


def _idea_assessment(status: ComplianceStatus, confidence: float) -> ComplianceAssessment:
    return ComplianceAssessment(status=status, justification="j" * 30, confidence=confidence)


async def test_low_confidence_rejection_is_not_upgraded_to_flag_for_review(
    ctx: FactoryContext, gemini_fake: FakeProvider, settings: Settings
) -> None:
    """A REJECTED verdict is already the safe, conservative outcome -- unlike an
    APPROVED one, it must never be escalated to FLAG_FOR_REVIEW just because the
    model's stated confidence fell under the threshold. Doing so used to leave an
    already-decided idea sitting in the pipeline awaiting a human reply that was
    never actually needed."""
    assert settings.compliance_min_confidence == 0.85
    gemini_fake.register(ComplianceAssessment, _idea_assessment(ComplianceStatus.REJECTED, 0.1))

    result = await assessor.assess_idea(ctx, _IDEA)

    assert result.status is ComplianceStatus.REJECTED
    assert result.approved is False


async def test_low_confidence_approval_is_still_downgraded_to_flag_for_review(
    ctx: FactoryContext, gemini_fake: FakeProvider, settings: Settings
) -> None:
    """Regression guard for the fix above: only REJECTED is exempt from the
    confidence bar. An uncertain APPROVED verdict must still be downgraded --
    approving requires being sure."""
    assert settings.compliance_min_confidence == 0.85
    gemini_fake.register(ComplianceAssessment, _idea_assessment(ComplianceStatus.APPROVED, 0.1))

    result = await assessor.assess_idea(ctx, _IDEA)

    assert result.status is ComplianceStatus.FLAG_FOR_REVIEW
    assert result.approved is False


async def test_high_confidence_rejection_stays_rejected(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    gemini_fake.register(ComplianceAssessment, _idea_assessment(ComplianceStatus.REJECTED, 0.95))

    result = await assessor.assess_idea(ctx, _IDEA)

    assert result.status is ComplianceStatus.REJECTED


async def test_high_confidence_approval_stays_approved(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    gemini_fake.register(ComplianceAssessment, _idea_assessment(ComplianceStatus.APPROVED, 0.95))

    result = await assessor.assess_idea(ctx, _IDEA)

    assert result.status is ComplianceStatus.APPROVED
    assert result.approved is True


# --- idea assessment must never become a memory-mutation channel ------------
#
# `assessor.py`'s own comment on `assess_idea` explains why: injecting
# model-authored memory text into a safety-critical, mutation-excluded prompt
# would be a de-facto way to mutate it outside `learning/store`'s promotion
# path. These assert the actual behaviour, not just the comment's claim.


async def test_idea_system_prompt_reaches_the_model_byte_identical(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    gemini_fake.register(ComplianceAssessment, _idea_assessment(ComplianceStatus.APPROVED, 0.95))

    await assessor.assess_idea(ctx, _IDEA)

    call = gemini_fake.calls[-1]
    assert call["system"] == assessor.IDEA_SYSTEM_PROMPT


def test_compliance_assessor_module_never_imports_memory_store() -> None:
    """A real, enforced check, not just a convention: `prior_verdicts_for_niche`
    (structured facts from `ideas`) is the only context this module is allowed
    to add, so `factory.memory` must never appear in it at all."""
    import inspect

    source = inspect.getsource(assessor)
    assert "memory_store" not in source
    assert "factory.memory" not in source


# --- preflight --------------------------------------------------------------


def test_missing_database_is_a_hard_block(tmp_path: Any, blacklist_file: Any) -> None:
    bare = Settings(
        database_url=None,
        gemini_api_key=None,
        anthropic_api_key=None,
        blacklist_path=blacklist_file,
        workspace_root=tmp_path,
    )
    report = run_preflight(bare)
    subjects = {f.subject for f in report.hard_blocks}
    assert "DATABASE_URL" in subjects
    assert "LLM_PROVIDER" in subjects
    assert report.ok is False


def test_one_provider_key_satisfies_the_provider_requirement(
    tmp_path: Any, blacklist_file: Any
) -> None:
    partial = Settings(
        database_url="postgresql+asyncpg://x/y",
        gemini_api_key="key",
        anthropic_api_key=None,
        blacklist_path=blacklist_file,
        workspace_root=tmp_path,
    )
    report = run_preflight(partial)
    assert "LLM_PROVIDER" not in {f.subject for f in report.hard_blocks}
    # Development still degrades: no Claude key means no code generation.
    assert EngineName.DEVELOPMENT in report.disabled_engines


def test_missing_blacklist_is_a_hard_block(tmp_path: Any) -> None:
    """A silently absent suppression list is a compliance incident waiting to happen."""
    report = run_preflight(
        Settings(
            database_url="postgresql+asyncpg://x/y",
            gemini_api_key="key",
            blacklist_path=tmp_path / "nope.txt",
            workspace_root=tmp_path,
        )
    )
    assert "BLACKLIST" in {f.subject for f in report.hard_blocks}


def test_degradations_are_written_where_a_human_can_read_them(
    tmp_path: Any, blacklist_file: Any
) -> None:
    """Telegram may itself be the missing credential, so the report goes to disk."""
    report = run_preflight(
        Settings(
            _env_file=None,  # hermetic: a real developer .env must not leak in here
            database_url="postgresql+asyncpg://x/y",
            gemini_api_key="key",
            blacklist_path=blacklist_file,
            workspace_root=tmp_path,
        )
    )
    target = tmp_path / "MISSING_CREDENTIALS.md"
    written = write_missing_credentials(report, target)
    assert written == target
    text = target.read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN" in text


# --- sandbox workspace sharing (sibling-container path alignment) -----------


def test_sharing_check_passes_when_the_sandbox_can_write_where_the_factory_reads(
    tmp_path: Any, blacklist_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://x/y",
        gemini_api_key="key",
        blacklist_path=blacklist_file,
        workspace_root=tmp_path,
    )

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        # Simulates a sandbox container that successfully wrote into the shared
        # bind-mounted directory — the same path this process resolves too.
        probe_dir = tmp_path / ".preflight-probe"
        (probe_dir / "marker").write_text("ok\n", encoding="utf-8")
        return MagicMock(returncode=0)

    monkeypatch.setattr("factory.preflight.subprocess.run", fake_run)
    assert check_sandbox_workspace_sharing(settings) is None


def test_sharing_check_fails_when_the_sandbox_writes_somewhere_else(
    tmp_path: Any, blacklist_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact failure mode of a named volume or mismatched bind-mount path: the
    container call "succeeds" but the marker never appears where the factory looks."""
    settings = Settings(
        database_url="postgresql+asyncpg://x/y",
        gemini_api_key="key",
        blacklist_path=blacklist_file,
        workspace_root=tmp_path,
    )

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        return MagicMock(returncode=0)  # ran "successfully", wrote nowhere this process sees

    monkeypatch.setattr("factory.preflight.subprocess.run", fake_run)
    finding = check_sandbox_workspace_sharing(settings)

    assert finding is not None
    assert finding.subject == "SANDBOX_WORKSPACE_SHARING"
    assert finding.severity is Severity.DEGRADES


def test_sharing_check_degrades_rather_than_raises_when_docker_itself_errors(
    tmp_path: Any, blacklist_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://x/y",
        gemini_api_key="key",
        blacklist_path=blacklist_file,
        workspace_root=tmp_path,
    )

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        raise OSError("docker: command not found")

    monkeypatch.setattr("factory.preflight.subprocess.run", fake_run)
    finding = check_sandbox_workspace_sharing(settings)

    assert finding is not None
    assert finding.severity is Severity.DEGRADES


def test_a_broken_sandbox_share_disables_development_with_a_specific_reason(
    tmp_path: Any, blacklist_file: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://x/y",
        gemini_api_key="key",
        anthropic_api_key="key",
        blacklist_path=blacklist_file,
        workspace_root=tmp_path,
    )
    broken = Finding(
        subject="SANDBOX_WORKSPACE_SHARING",
        severity=Severity.DEGRADES,
        detail="mismatch",
        remedy="fix the mount",
    )
    monkeypatch.setattr("factory.preflight.docker_available", lambda: True)
    monkeypatch.setattr("factory.preflight.check_sandbox_workspace_sharing", lambda s: broken)

    report = run_preflight(settings)

    assert "sandbox workspace" in report.disabled_engines[EngineName.DEVELOPMENT]
    assert any(f.severity is Severity.DEGRADES for f in report.degradations)


# ------------------------------------------- what it costs to ask another department


def _facts_missing_design():
    """A real product with no design standard and nothing deployed.

    Built from the actual columns rather than by setting `unmet` -- that is a derived
    property, and a fixture that could set it directly would let these tests pass
    against a `unmet` that no product could ever really be in.
    """
    from factory.facts import ProductFacts

    return ProductFacts(
        product_id="p-1",
        idea_id="i-1",
        name="Driftwatch",
        status="planned",
        audience_sentence="engineering leads at small software companies",
        audience_evidence=3,
        claimed_evidence_count=3,
        has_requirements=False,
        has_design_guidelines=False,
        deploy_url=None,
        interest=0,
        reach=0,
        open_issues=0,
    )


def test_a_need_another_department_owns_says_what_asking_costs() -> None:
    """The missing half of the trade-off, 2026-09-22.

    The decider was shown `design_guidelines` as startable and told in plain words
    that a build *reads* the design standard and falls back to the generic house
    style. It chose to build anyway, three times running. That is not a reasoning
    failure: the alternative was an unbounded wait against a bounded action, and the
    bounded one wins that comparison however well it is reasoned about.

    Asking actually cost thirty seconds -- Design's manager tick to guidelines on
    disk, measured on that run. Nothing in the menu said so, so a cheap option looked
    expensive and the product was built blind, then restyled hours later against a
    standard that had moved underneath it.
    """
    from factory.facts import OWNER
    from factory.state.repository import Responsiveness
    from factory.supervisor.product_decision import _render_options

    need = next(iter(OWNER))
    engine, kind = OWNER[need]
    facts = _facts_missing_design()

    rendered = _render_options(
        facts, {kind.value: Responsiveness(kind=kind.value, observations=4, median_seconds=31.0)}
    )

    # The need is presented as somebody else's, and what asking costs is on the line.
    assert "Another department's to do" in rendered
    asking = next(line for line in rendered.splitlines() if line.strip().startswith("Asking"))
    assert engine.value in asking
    assert "31 seconds" in asking
    assert "median of 4" in asking
    # ...and both options stay open. The menu prices the wait; it never spends the
    # judgement. Building first is still a perfectly good answer for a product the
    # house style genuinely suits, which is the whole reason this is a fact about the
    # company rather than an ordering rule.
    assert "whether it is worth waiting" in rendered
    commissionable = rendered.split("Another department")[0]
    assert "- deployment:" in commissionable


def test_never_having_asked_is_said_plainly_rather_than_guessed() -> None:
    """Zero observations is a real answer and a different one from "they are slow".
    Inventing a number here would be the constant this measurement exists to avoid."""
    from factory.facts import OWNER
    from factory.state.repository import Responsiveness
    from factory.supervisor.product_decision import _render_options

    need = next(iter(OWNER))
    _, kind = OWNER[need]
    facts = _facts_missing_design()

    rendered = _render_options(
        facts, {kind.value: Responsiveness(kind=kind.value, observations=0, median_seconds=None)}
    )
    assert "nobody has asked them before" in rendered


def test_every_owned_need_can_be_priced() -> None:
    """The rule, not the values -- the shape that would have caught DESIGN missing
    from routing.PRIORITY. A need added to OWNER without its manager's task kind
    being measurable would silently render "not recorded" forever."""
    from factory.facts import OWNER
    from factory.state.schemas import TaskKind

    for need, (_, kind) in OWNER.items():
        assert isinstance(kind, TaskKind), f"{need} has no measurable manager task"
