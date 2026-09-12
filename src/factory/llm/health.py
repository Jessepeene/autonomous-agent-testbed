"""Provider circuit breaker: notice a sustained outage and pause on it.

The gap this closes, observed live twice. A provider goes down for a reason that
will never self-resolve without a human -- `402 Insufficient Balance`,
`401 Unauthorized`, an expired card -- and the factory keeps dispatching work to
it forever. The individual failure handling is all correct in isolation: a
repair blocked by an outage is deferred rather than failed (an outage says
nothing about whether the product is fixable), and a deferred task does not count
toward `max_task_attempts`. Composed, those two correct rules produce infinite
silent retrying: one real repair task retried 40 times over seven hours against a
provider returning 402 on every call, spending real latency and agent-loop setup
each time, and raising no interrupt because nothing ever "failed" enough to
escalate.

The fix is the shape this codebase already uses twice -- `orchestrator._check_budget`
(spend cap) and `marketing/deliverability.py` (complaint-rate freeze): a standing
state that pauses a class of work, tells a human once, and clears itself when the
condition passes.

**Health is derived, not stored.** There is no breaker table and no state machine
to drift: `llm_calls` already records every call's provider and outcome, so
"has this provider succeeded at all recently" is one query. That makes the
breaker self-healing for free -- the moment a real call succeeds, or the failures
simply age out of the window, the provider is no longer tripped. Nothing has to
remember to reset it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.llm.providers.base import Provider
from factory.logging import get_logger
from factory.state.orm import LLMCall

log = get_logger(__name__)

#: How far back to judge a provider. Long enough that a brief blip does not trip
#: the breaker, short enough that recovery is noticed within a few ticks.
WINDOW_MINUTES = 30

#: Below this many calls in the window there is not enough evidence to call it an
#: outage -- one failed call on a quiet provider proves nothing.
MIN_CALLS = 8


async def tripped_providers(
    session: AsyncSession,
    *,
    window_minutes: int = WINDOW_MINUTES,
    min_calls: int = MIN_CALLS,
) -> dict[Provider, str]:
    """Providers that have failed every recent call, mapped to a human-readable
    reason.

    The test is deliberately "zero successes", not a failure *rate*: a provider
    that is half working is degraded, and the retry/failover machinery already
    handles degraded correctly. What that machinery cannot handle is a provider
    that is wholly, persistently gone -- which is exactly the case worth pausing
    on, and exactly the case a rate threshold would take longest to detect.
    """
    since = datetime.now(UTC) - timedelta(minutes=window_minutes)
    stmt = (
        select(
            LLMCall.provider,
            func.count().label("total"),
            func.count().filter(LLMCall.outcome == "ok").label("succeeded"),
        )
        .where(LLMCall.created_at >= since)
        .group_by(LLMCall.provider)
    )
    tripped: dict[Provider, str] = {}
    for provider_name, total, succeeded in (await session.execute(stmt)).all():
        if total < min_calls or succeeded:
            continue
        try:
            provider = Provider(provider_name)
        except ValueError:  # pragma: no cover - a provider no longer in the enum
            continue
        tripped[provider] = f"{total} call(s) in the last {window_minutes}m, none succeeded"
    return tripped


def describe(tripped: dict[Provider, str]) -> str:
    return ", ".join(f"{p.value} ({reason})" for p, reason in sorted(tripped.items()))
