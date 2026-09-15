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

#: Successes required before a tripped provider counts as healthy again.
#:
#: The hysteresis, and it is load-bearing. The first version tripped on "no
#: successes" but cleared on *any* success, so a provider returning
#: `429 RESOURCE_EXHAUSTED` that intermittently let one call through oscillated:
#: tripped 23:06, recovered 23:34, tripped again 00:06 -- and because the notice
#: auto-closes on recovery, each cycle raised a brand-new escalation. One unchanged
#: condition would have produced an escalation every half hour overnight, which is
#: the same storm this breaker exists to prevent, wearing a different hat.
#:
#: Asymmetric on purpose: one failure is not enough to trip, and one success is not
#: enough to clear. A provider genuinely back up produces successes easily; a
#: flapping one does not.
MIN_RECOVERY_SUCCESSES = 3

#: Outcomes that say nothing about whether the *provider* is reachable.
#:
#: This breaker answers one question: is this vendor wholly, persistently gone? A
#: schema violation is the opposite kind of evidence -- the provider accepted the
#: request, billed for it, and returned a complete response that happened not to
#: satisfy a Pydantic model. Counting that as an outage produces a deadlock:
#: the breaker pauses a healthy provider, and pausing changes nothing, because the
#: model will fail the same schema whenever it resumes.
#:
#: Observed live within an hour of the POC starting. `deepseek-flash` returned
#: HTTP 200 to every call and missed the schema on most orchestration and analysis
#: requests; the breaker declared "8 call(s) in the last 30m, none succeeded",
#: paused DeepSeek -- which under POC routing is nearly every task type -- and the
#: factory stopped with a perfectly reachable provider.
#:
#: A model that cannot hold a schema is a real problem and a different one, fixed
#: by changing model or task routing, never by waiting.
NON_TRANSPORT_OUTCOMES = ("schema_violation", "schema_violation_repaired", "refusal")


async def tripped_providers(
    session: AsyncSession,
    *,
    window_minutes: int = WINDOW_MINUTES,
    min_calls: int = MIN_CALLS,
    min_recovery_successes: int = MIN_RECOVERY_SUCCESSES,
) -> dict[Provider, str]:
    """Providers in a sustained outage, mapped to a human-readable reason.

    Not a failure *rate*: a provider that is half working is degraded, and the
    retry/failover machinery already handles degraded correctly. What that
    machinery cannot handle is a provider that is wholly, persistently gone.

    "Wholly gone" allows a couple of stray successes, because that is what a
    depleted-quota provider actually looks like from outside -- see
    MIN_RECOVERY_SUCCESSES.
    """
    since = datetime.now(UTC) - timedelta(minutes=window_minutes)
    stmt = (
        select(
            LLMCall.provider,
            func.count().label("total"),
            func.count().filter(LLMCall.outcome == "ok").label("succeeded"),
        )
        # Only transport evidence decides reachability. A call the provider answered
        # -- and charged for -- is proof it is up, whatever the body turned out to
        # contain, so schema violations and refusals are excluded from the
        # population entirely rather than counted as failures. See
        # NON_TRANSPORT_OUTCOMES.
        .where(LLMCall.created_at >= since, LLMCall.outcome.notin_(NON_TRANSPORT_OUTCOMES))
        .group_by(LLMCall.provider)
    )
    tripped: dict[Provider, str] = {}
    for provider_name, total, succeeded in (await session.execute(stmt)).all():
        if total < min_calls or succeeded >= min_recovery_successes:
            continue
        failed = total - succeeded
        if failed < min_calls:
            # Mostly quiet rather than mostly failing -- not enough failure to call
            # it an outage, whatever the success count happens to be.
            continue
        try:
            provider = Provider(provider_name)
        except ValueError:  # pragma: no cover - a provider no longer in the enum
            continue
        tripped[provider] = (
            f"{failed} of {total} call(s) failed in the last {window_minutes}m"
            if succeeded
            else f"{total} call(s) in the last {window_minutes}m, none succeeded"
        )
    return tripped


async def recovered_providers(
    session: AsyncSession,
    previously_tripped: set[Provider],
    *,
    window_minutes: int = WINDOW_MINUTES,
    min_recovery_successes: int = MIN_RECOVERY_SUCCESSES,
) -> set[Provider]:
    """Of the providers that were tripped, which have actually answered since.

    **Recovery needs positive evidence, and "no longer tripped" is not evidence.**
    That distinction is the whole function, and its absence ran in production for
    three days: the caller used to compute recovery as `previous - tripped`, plain
    set subtraction.

    `tripped_providers` returns nothing for a provider with fewer than `MIN_CALLS`
    in the window -- correctly, since a handful of failures on a quiet provider
    proves nothing. But subtraction cannot tell *that* absence from a real recovery,
    so a dead provider that simply went quiet dropped out of the tripped set and was
    announced to the operator as "answering again".

    Gemini did exactly this. Depleted credits from 2026-09-12, **zero successful
    calls ever**, 2-11 calls per hour against a 30-minute window -- so it fell out of
    the tripped set constantly. Eight separate outage escalations were raised and
    seven auto-resolved with the message "gemini is answering again", about a
    provider that had never once answered. Each closure wiped the evidence, so three
    days of total outage read as a series of brief blips that all fixed themselves.

    Quiet is not healthy. A provider genuinely back up produces successes easily.
    """
    if not previously_tripped:
        return set()

    since = datetime.now(UTC) - timedelta(minutes=window_minutes)
    stmt = (
        select(LLMCall.provider, func.count().label("succeeded"))
        .where(
            LLMCall.created_at >= since,
            LLMCall.outcome == "ok",
            LLMCall.provider.in_([p.value for p in previously_tripped]),
        )
        .group_by(LLMCall.provider)
    )
    recovered: set[Provider] = set()
    for provider_name, succeeded in (await session.execute(stmt)).all():
        if succeeded < min_recovery_successes:
            continue
        try:
            recovered.add(Provider(provider_name))
        except ValueError:  # pragma: no cover - a provider no longer in the enum
            continue
    return recovered


def describe(tripped: dict[Provider, str]) -> str:
    return ", ".join(f"{p.value} ({reason})" for p, reason in sorted(tripped.items()))
