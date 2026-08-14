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

from sqlalchemy.ext.asyncio import AsyncSession

from factory.hitl import interrupts
from factory.logging import get_logger
from factory.state import repository
from factory.state.schemas import InterruptReason, ScoredIdea

log = get_logger(__name__)

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
