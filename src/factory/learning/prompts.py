"""The read API engines call instead of using a bare module constant directly.

Behaviour is bit-for-bit identical to a hardcoded constant until something is actually
proposed and confirmed: the first call for any key bootstraps an ``active`` row from
the ``default`` the caller passes in, and every call after that returns whatever is
currently active — which is still that same default, until a trial gets confirmed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from factory.learning import store
from factory.learning.allowlist import default_metric_for


async def get_prompt(session: AsyncSession, *, key: str, default: str) -> tuple[str, str]:
    """Returns ``(text, tunable_parameter_id)`` — the trial version if one is
    currently running for this key, otherwise the active version."""
    row = await store.get_serving(session, key)
    if row is not None:
        return str(row.value["text"]), row.id
    version_id = await store.bootstrap_active(
        session,
        kind="PROMPT",
        key=key,
        value={"text": default},
        created_by="bootstrap",
        target_metric=default_metric_for(key),
    )
    return default, version_id


async def get_scoring_weights(
    session: AsyncSession, *, key: str, default: dict[str, float]
) -> tuple[dict[str, float], str]:
    """Returns ``(weights, tunable_parameter_id)`` — the trial version if one is
    currently running for this key, otherwise the active version."""
    row = await store.get_serving(session, key)
    if row is not None:
        return {str(k): float(v) for k, v in row.value.items()}, row.id
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
