"""The read API engines call instead of using a bare module constant directly.

Behaviour is bit-for-bit identical to a hardcoded constant until something is actually
proposed and confirmed: the first call for any key bootstraps an ``active`` row from
the ``default`` the caller passes in, and every call after that returns whatever is
currently active — which is still that same default, until a trial gets confirmed.

That identity used to hold only for the instant of the bootstrap. Editing the code
constant afterwards — an ordinary thing to do, and the whole reason it lives in a file
— left the stored copy serving the old text with nothing anywhere comparing the two,
in either direction. So the read below now compares on every call, and resolves the
disagreement by provenance rather than by precedence: a copy of the code follows the
code; a value that won a trial outranks it and says so.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from factory.learning import store
from factory.learning.allowlist import default_metric_for
from factory.logging import get_logger
from factory.state.orm import TunableParameter

log = get_logger(__name__)


async def get_prompt(session: AsyncSession, *, key: str, default: str) -> tuple[str, str]:
    """Returns ``(text, tunable_parameter_id)`` — the trial version if one is
    currently running for this key, otherwise the active version.

    ``default`` is not only the seed for the very first read. It is the code constant
    *as it stands right now*, and it is compared against the served text on every call.
    The two can disagree for two entirely unrelated reasons, and which one wins depends
    on how the served text came to be — a value has to say how it is known:

    * a **bootstrap** row is a copy of the code constant and nothing more. If the
      constant has since been edited, the copy is stale; the code wins and the row is
      re-synced.
    * a **learned** row (a confirmed trial, or a trial currently in flight) differs
      from the code on purpose, because it beat the alternative on its target metric.
      The row wins — but the code baseline moving underneath it is a fact somebody has
      to reconcile, so it escalates rather than being silently absorbed.

    The re-sync additionally waits while any candidate or trial is open on the key,
    since both are proposals measured *against the text being replaced*.

    Without this comparison the disagreement is undetectable from either side: the
    engine sees a prompt and has no idea it is not the one in the file, and the file
    reads as if it were in force. ``get_scoring_weights`` below has always made the
    equivalent check, but only because a mismatched *weights* dict corrupts a score
    loudly; a stale *prompt* just quietly produces worse work.
    """
    row = await store.get_serving(session, key)
    if row is not None:
        served = str(row.value["text"])
        if served == default:
            return served, row.id
        resynced = await _reconcile_with_code(
            session,
            key=key,
            kind="PROMPT",
            row=row,
            value={"text": default},
            served_desc=f"{len(served)} characters",
            code_desc=f"{len(default)} characters",
        )
        if resynced is not None:
            return default, resynced
        return served, row.id
    version_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=key,
        value={"text": default},
        created_by="bootstrap",
        target_metric=default_metric_for(key),
    )
    return default, version_id


async def _reconcile_with_code(
    session: AsyncSession,
    *,
    key: str,
    kind: str,
    row: TunableParameter,
    value: dict[str, Any],
    served_desc: str,
    code_desc: str,
) -> str | None:
    """The served value and the code constant disagree. Decide which one wins.

    Returns the id of a freshly re-synced version, or ``None`` to keep serving ``row``.
    Prompts and scoring weights take the same route here on purpose: the question is
    never "which value is better", it is "what is each value's claim to being served",
    and that is answered identically for both.
    """
    is_copy_of_code = (
        row.status == "active" and row.created_by == "bootstrap" and row.parent_id is None
    )
    if is_copy_of_code:
        if await store.has_open_work(session, key):
            # A candidate is waiting, or a trial is running, and either was authored as
            # an edit *to this value*. Retiring it now would leave the comparison
            # measuring a pair that never coexisted. The wait is bounded by the
            # promotion cooldown and the trial window, and the read right after that
            # resolves re-syncs — so this defers the fix, it never drops it.
            log.warning(
                "learning.resync_deferred_by_open_work",
                key=key,
                tunable_parameter_id=row.id,
                served=served_desc,
                code=code_desc,
            )
            return None
        return await store.resync_bootstrap(
            session,
            kind=kind,
            key=key,
            value=value,
            target_metric=default_metric_for(key),
            stale=row,
        )
    await _report_baseline_drift(
        session, key=key, row=row, served_desc=served_desc, code_desc=code_desc
    )
    return None


async def _report_baseline_drift(
    session: AsyncSession, *, key: str, row: TunableParameter, served_desc: str, code_desc: str
) -> None:
    """A learned version is serving, and the code constant it was learned against has
    since been edited. Neither value is wrong, which is exactly why a human has to pick:
    the learned edit may no longer make sense on top of the new baseline, and the new
    baseline's intent is not reaching production while the learned version serves.

    Imported here rather than at module scope to keep ``learning`` importable from
    ``supervisor.audit``, which already reaches the other way round.
    """
    from factory.hitl import interrupts
    from factory.state.schemas import InterruptReason

    log.warning(
        "learning.baseline_drift",
        key=key,
        tunable_parameter_id=row.id,
        status=row.status,
        served=served_desc,
        code=code_desc,
    )
    await interrupts.request(
        session,
        reason=InterruptReason.DATA_INTEGRITY,
        subject=f"{key} has drifted from the code constant",
        detail=(
            f"The version currently serving {key} (id={row.id}, v{row.version}, "
            f"status={row.status}, created_by={row.created_by}) is a learned version — "
            f"it won its trial against an earlier code baseline — but that baseline has "
            f"since been edited in the repository.\n\n"
            f"Served: {served_desc}. Code constant now: {code_desc}.\n\n"
            "The learned version keeps serving, because it is the one with evidence "
            "behind it. But nothing written into the code constant since that trial is "
            "reaching production, and nobody downstream can tell. Either re-propose the "
            "learned edit on top of the new baseline, or retire it so the code serves."
        ),
        headline="🧬 A served value no longer matches the code it was learned from",
        reply_hint="No reply needed — but the two versions need reconciling by hand.",
        needs_reply=False,
    )


def _describe_weights(weights: dict[str, float]) -> str:
    """Weights are short enough to state in full, and a drift report that says only
    "the numbers differ" is the kind of message that gets closed unread."""
    return ", ".join(f"{dim}={weights[dim]:g}" for dim in sorted(weights))


async def get_scoring_weights(
    session: AsyncSession, *, key: str, default: dict[str, float]
) -> tuple[dict[str, float], str]:
    """Returns ``(weights, tunable_parameter_id)`` — the trial version if one is
    currently running for this key, otherwise the active version.

    Guards against a served row whose dimension set doesn't match ``default``'s —
    e.g. a schema change (new scoring dimension) shipped without its migration ever
    running against this database. Falls back to the code default rather than
    raising: a dimension-mismatched weights dict would silently zero out every
    dimension `weighted_score` can't find (``dict.get(dim, 0.0)``), corrupting every
    score computed from it, and Discovery must degrade, never crash, on a contract
    mismatch it didn't cause. See `supervisor.audit.check_scoring_contract_drift`
    for the alert this same mismatch also raises.

    A *dimension* mismatch and a *value* drift are two different facts and are handled
    separately below. The first is a broken contract — the served row cannot be used at
    all, whatever its provenance. The second is the same question ``get_prompt`` asks:
    the dimensions agree, the numbers do not, and which wins depends on whether the
    served row is a copy of the code or something that outscored it.
    """
    row = await store.get_serving(session, key)
    if row is not None:
        served = {str(k): float(v) for k, v in row.value.items()}
        if set(served) != set(default):
            log.error(
                "learning.scoring_weights_contract_drift",
                key=key,
                tunable_parameter_id=row.id,
                served_dimensions=sorted(served),
                expected_dimensions=sorted(default),
            )
            return dict(default), row.id
        if served != default:
            resynced = await _reconcile_with_code(
                session,
                key=key,
                kind="SCORING_WEIGHTS",
                row=row,
                value=dict(default),
                served_desc=_describe_weights(served),
                code_desc=_describe_weights(default),
            )
            if resynced is not None:
                return dict(default), resynced
        return served, row.id
    version_id = await store.bootstrap_active(
        session,
        kind="SCORING_WEIGHTS",
        key=key,
        value=dict(default),
        created_by="bootstrap",
        target_metric=default_metric_for(key),
    )
    return default, version_id


async def record_outcome(
    session: AsyncSession,
    *,
    tunable_parameter_id: str,
    success: bool,
    detail: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> None:
    """Thin, readable wrapper over ``store.record_episode`` for call sites that only
    ever deal in booleans, not the raw outcome string."""
    await store.record_episode(
        session,
        tunable_parameter_id=tunable_parameter_id,
        outcome="success" if success else "failure",
        detail=detail,
        task_id=task_id,
    )
