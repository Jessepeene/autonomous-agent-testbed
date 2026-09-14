"""Deterministic checks on the assumptions Orchestrator-facing snapshot metrics
silently rely on.

`build_snapshot`'s `approved_ideas_waiting` (`orchestrator.py`) trusted, for a
long time, that every idea marked 'approved' has a product -- Compliance always
creates one in the same pass an idea is approved. That assumption held right up
until it didn't, and nothing checked it: the query just joined and filtered on
product status, which would silently undercount rather than ever flag a
violation. The learning loop tunes *values* within a fixed measurement
framework (is this prompt version producing better outcomes); it has no way to
audit the framework itself. This module is that other half: not tuning
anything, just periodically re-verifying that a load-bearing assumption still
holds, independent of whether the metric's own query would ever notice it broke.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from factory.hitl import interrupts
from factory.logging import get_logger
from factory.state import repository
from factory.state.schemas import InterruptReason, ScoredIdea

log = get_logger(__name__)

#: Past this many days with no product changing status at all, "quiet" stops
#: being a plausible description of a healthy factory and starts looking like
#: "stuck" -- worth a human's attention the way an owner who hasn't seen a
#: single order all week would start asking questions.
_STUCK_FACTORY_DAYS = 3
#: Several *different* products all hitting a heal cycle within a day is a
#: pattern worth naming out loud -- one product cycling repeatedly is routine.
_HEAL_SPIKE_PRODUCT_THRESHOLD = 3
#: How many multiples of the trailing week's daily average today's spend has
#: to clear before it's worth a look -- independent of and much looser than
#: `daily_llm_spend_cap_usd`'s hard breaker (orchestrator.py), which already
#: stops real work; this is a softer, earlier "does this look right" signal.
_SPEND_SPIKE_MULTIPLIER = 3.0

#: Same literal as `discovery/graph.py`/`discovery/score.py`'s own copy -- this
#: codebase's convention is each module holds its own copy of an allowlist key
#: string rather than importing another module's private constant across engines.
_SCORING_WEIGHTS_KEY = "factory.engines.discovery.score:SCORING_WEIGHTS"


async def check_approved_idea_orphans(session: AsyncSession) -> int:
    """Raise one interrupt if any approved idea has no product at all.

    Not urgent -- nothing crashes and no money moves -- but it means a
    guarantee this factory's own routing logic depends on has quietly stopped
    holding, which is worth a human's attention rather than a silent
    undercount discovered weeks later. Deduped by `interrupts.request`'s usual
    subject-collapse: repeated detection while the same problem persists
    raises exactly one open interrupt, not one per check.
    """
    orphans = await repository.orphaned_approved_ideas(session)
    if not orphans:
        return 0

    lines = [f"- {idea.id} ({idea.target_niche[:80]})" for idea in orphans[:10]]
    detail = (
        f"{len(orphans)} idea(s) are marked 'approved' with no product at all:\n\n"
        + "\n".join(lines)
        + (f"\n... and {len(orphans) - 10} more" if len(orphans) > 10 else "")
        + "\n\nCompliance is supposed to create a product in the same pass an "
        "idea is approved -- this means that guarantee broke somewhere, and any "
        "metric that assumes it (approved_ideas_waiting) is undercounting these. "
        "Not urgent, but worth investigating."
    )
    await interrupts.request(
        session,
        reason=InterruptReason.DATA_INTEGRITY,
        subject="Approved idea(s) exist with no product",
        detail=detail,
        headline="📋 Data integrity notice",
        reply_hint="No reply needed — informational only, but worth a look when you have time.",
        needs_reply=False,
    )
    log.warning("audit.approved_idea_orphans", count=len(orphans))
    return len(orphans)


async def check_scoring_contract_drift(session: AsyncSession) -> bool:
    """Raise one interrupt if the ``SCORING_WEIGHTS`` row currently serving
    Discovery doesn't carry every dimension ``ScoredIdea.weighted_score`` expects.

    ``learning/prompts.py::get_scoring_weights`` already guards this at read time
    by silently falling back to the code default -- correct, since Discovery must
    degrade rather than crash on a contract mismatch it didn't cause, but silent
    by design. That silence is exactly what let a stale served prompt turn into an
    undetected outage the last time this codebase's own risk audit found one. This
    is the other half of that fix: the same check, surfaced to a human instead of
    only ever swallowed.
    """
    from factory.learning import store as learning_store

    row = await learning_store.get_serving(session, _SCORING_WEIGHTS_KEY)
    if row is None:
        return False  # never bootstrapped yet -- nothing to have drifted from

    served = set(row.value)
    expected = set(ScoredIdea.SCORE_DIMENSIONS)
    if served == expected:
        return False

    detail = (
        f"The tunable_parameters row currently serving SCORING_WEIGHTS "
        f"(id={row.id}, status={row.status}) has dimensions {sorted(served)}, but "
        f"ScoredIdea.SCORE_DIMENSIONS expects {sorted(expected)}.\n\n"
        "get_scoring_weights() is already falling back to the code default so "
        "Discovery keeps running, but every idea is being scored against defaults "
        "rather than whatever this row actually holds -- likely a migration that "
        "was meant to rewrite this row never ran against this database."
    )
    await interrupts.request(
        session,
        reason=InterruptReason.DATA_INTEGRITY,
        subject="SCORING_WEIGHTS dimension mismatch",
        detail=detail,
        headline="⚖️ Scoring weights contract drift",
        reply_hint="No reply needed — informational only, but worth investigating.",
        needs_reply=False,
    )
    log.warning("audit.scoring_contract_drift", tunable_parameter_id=row.id)
    return True


async def check_business_health(
    session: AsyncSession, *, extra_concerns: list[str] | None = None
) -> list[str]:
    """Tier 0 finding #0g, system-wide verification plan: the other two checks
    in this module each re-verify one specific load-bearing invariant. This is
    the more general instinct neither one covers -- a founder periodically
    stepping back from the day-to-day and asking "does the business, taken as
    a whole, look healthy," rather than checking one specific fact. Three
    plain, deterministic signals, same spirit as the checks above: no LLM
    judgement anywhere in this function. Returns the concerns found (empty if
    none), and raises exactly one interrupt covering all of them together --
    several unrelated-looking oddities on the same day are worth reading as
    one report, not three separate pings.
    """
    # Findings from other deterministic checks that should reach the same report.
    # `check_pipeline_completeness` passes its gaps here rather than raising a
    # second interrupt: a day with three unrelated-looking oddities is one report,
    # not three pings, which is the same reasoning this function already applies
    # to its own three signals.
    concerns: list[str] = list(extra_concerns or [])
    now = datetime.now(UTC)

    last_activity = await repository.most_recent_product_activity(session)
    if last_activity is not None:
        idle_days = (now - last_activity).days
        if idle_days >= _STUCK_FACTORY_DAYS:
            concerns.append(
                f"No product has changed status in {idle_days} day(s) "
                f"(last activity: {last_activity.isoformat()})."
            )

    recent_heals = await repository.products_with_recent_heal_attempts(
        session, since=now - timedelta(hours=24)
    )
    if len(recent_heals) >= _HEAL_SPIKE_PRODUCT_THRESHOLD:
        concerns.append(
            f"{len(recent_heals)} different products hit a heal cycle in the last 24h "
            "-- possibly a systemic cause rather than unrelated bugs."
        )

    from factory.observability.costs import total_usd

    today_usd = await total_usd(session, hours=24)
    trailing_usd = await total_usd(session, hours=24 * 8)
    prior_week_usd = trailing_usd - today_usd
    baseline_usd = prior_week_usd / 7
    if baseline_usd > 0 and today_usd > baseline_usd * _SPEND_SPIKE_MULTIPLIER:
        concerns.append(
            f"Today's LLM spend (${today_usd:.2f}) is over {_SPEND_SPIKE_MULTIPLIER:g}x the "
            f"trailing week's daily average (${baseline_usd:.2f}), independent of the hard "
            "spend cap."
        )

    if not concerns:
        return concerns

    detail = "A few things looked statistically unusual today, worth a glance:\n\n" + "\n".join(
        f"- {c}" for c in concerns
    )
    await interrupts.request(
        session,
        reason=InterruptReason.DATA_INTEGRITY,
        subject=f"Business-health check, {now.date().isoformat()}",
        detail=detail,
        headline="🔎 Business health signals",
        reply_hint="No reply needed — informational only, but worth a look when you have time.",
        needs_reply=False,
    )
    log.info("audit.business_health_concerns", count=len(concerns))
    return concerns


#: How far back to look for products that shipped. Long enough that a stage which
#: quietly stopped running shows up within a day or two, short enough that a gap
#: already reported and accepted does not keep resurfacing forever.
_PIPELINE_AUDIT_DAYS = 7


async def check_pipeline_completeness(session: AsyncSession) -> list[str]:
    """Did the factory actually do all of its own steps?

    Every other check in this codebase verifies a *product*. This one verifies the
    *process*, and nothing did before -- which is exactly how a whole stage went
    missing without anyone noticing.

    The case that prompted it: `build_design_brief` sat below an early `return` in
    the validation branch, so the only product type this factory ships was the one
    that never reached it. Every landing page deployed with an empty design panel,
    for months, and no check anywhere could have said so. It took a human opening
    the dashboard and asking "why is there no design brief?".

    The general failure is that a stage which stops running leaves no trace, so
    absence of a finding is indistinguishable from absence of a check: the gap check
    files nothing when it finds nothing, and a gap check that never ran files
    nothing either. "Is the artifact there" is the only form of the question a
    broken stage cannot answer on its own behalf.

    Deterministic, no LLM judgement -- the same posture as every other check here.
    """
    since = datetime.now(UTC) - timedelta(days=_PIPELINE_AUDIT_DAYS)
    shipped = await repository.recently_shipped_products(session, since=since)
    if not shipped:
        return []

    # (what is missing, why it matters) -- phrased as a fault in the factory rather
    # than in the product, because that is what it is.
    stages = (
        (
            "acceptance criteria",
            "has_requirements",
            "nothing recorded what it was supposed to do",
        ),
        (
            "release decision",
            "has_release_decision",
            "no judgement was recorded before it went out",
        ),
        (
            "pre-ship review",
            "has_preship_review",
            "the deployed page was never opened and used by a reviewer",
        ),
        (
            "design brief",
            "has_design_brief",
            "no design brief was built from the live page",
        ),
    )

    concerns: list[str] = []
    for stage, attribute, why in stages:
        missing = [p.name[:40] for p in shipped if not getattr(p, attribute)]
        if not missing:
            continue
        # The count is what distinguishes a glitch from a wiring bug: one product
        # skipped is noise, all of them skipped means the stage is not running.
        concerns.append(
            f"{len(missing)} of {len(shipped)} product(s) shipped in the last "
            f"{_PIPELINE_AUDIT_DAYS} days with no {stage} -- {why}. "
            f"Affected: {', '.join(missing[:5])}."
        )

    if concerns:
        log.info("audit.pipeline_gaps", stages=len(concerns), shipped=len(shipped))
    return concerns
