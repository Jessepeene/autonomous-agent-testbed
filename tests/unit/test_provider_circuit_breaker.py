"""The per-provider circuit breaker.

The failure it exists to stop, observed live: DeepSeek returned `402 Insufficient
Balance` for seven hours while one real repair task retried 40 times. Every
individual rule was correct -- a repair blocked by an outage is *deferred* rather
than failed (an outage says nothing about whether the product is fixable), and a
deferred task does not count toward `max_task_attempts`. Composed, those two
correct rules produce infinite silent retrying against a provider that cannot
possibly succeed, and because nothing ever "failed", no interrupt was ever raised.

The breaker's contract, which these tests pin down:
- a tripped provider is never called at all (so an outage costs nothing),
- failover still works when only one provider is out,
- the call is refused only when every candidate is out,
- and the refusal is a distinct type, so callers can defer instead of escalating.
"""

from __future__ import annotations

import pytest

from factory.llm.client import LLMClient
from factory.llm.providers.base import Provider, ProviderCircuitOpen
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.settings import Settings, TaskType
from factory.state.schemas import Contract


class _Answer(Contract):
    text: str


def _client(settings: Settings, **providers: FakeProvider) -> LLMClient:
    registry = ProviderRegistry({Provider(name): p for name, p in providers.items()})
    return LLMClient(registry, settings=settings, backoff_base=0)


def test_no_tripped_providers_changes_nothing(settings: Settings) -> None:
    """The default is an empty set, so every existing caller is unaffected."""
    client = _client(settings, gemini=FakeProvider())
    assert client.tripped_providers == set()


async def test_a_tripped_sole_provider_refuses_without_calling_it(settings: Settings) -> None:
    """CODE_REPAIR is in `router.NO_FAILOVER` -- DeepSeek down means there is
    genuinely nothing to try, which is exactly the live case. The point is that no
    request is made: an outage should cost nothing, not 40 round trips."""
    deepseek = FakeProvider(name=Provider.DEEPSEEK)
    client = _client(settings, deepseek=deepseek)
    client.tripped_providers = {Provider.DEEPSEEK}

    with pytest.raises(ProviderCircuitOpen):
        await client.parse(
            _Answer, task_type=TaskType.CODE_REPAIR, system="s", prompt="p"
        )
    assert deepseek.calls == [], "a tripped provider must not be contacted at all"


async def test_failover_still_works_when_only_one_provider_is_tripped(
    settings: Settings,
) -> None:
    """A tripped Gemini must not take ANALYSIS down with it -- the breaker prunes
    candidates, it does not short-circuit the whole call."""
    gemini = FakeProvider(name=Provider.GEMINI)
    anthropic = FakeProvider(name=Provider.ANTHROPIC)
    anthropic.register(_Answer, _Answer(text="from anthropic"))
    client = _client(settings, gemini=gemini, anthropic=anthropic)
    client.tripped_providers = {Provider.GEMINI}

    result = await client.parse(_Answer, task_type=TaskType.ANALYSIS, system="s", prompt="p")

    assert result.text == "from anthropic"
    assert gemini.calls == []


async def test_every_candidate_tripped_refuses_the_call(settings: Settings) -> None:
    gemini = FakeProvider(name=Provider.GEMINI)
    anthropic = FakeProvider(name=Provider.ANTHROPIC)
    client = _client(settings, gemini=gemini, anthropic=anthropic)
    client.tripped_providers = {Provider.GEMINI, Provider.ANTHROPIC}

    with pytest.raises(ProviderCircuitOpen):
        await client.parse(_Answer, task_type=TaskType.ANALYSIS, system="s", prompt="p")
    assert gemini.calls == []
    assert anthropic.calls == []


async def test_a_tripped_provider_irrelevant_to_this_task_is_ignored(
    settings: Settings,
) -> None:
    """DeepSeek being down must not stop an ANALYSIS call that never routes there."""
    gemini = FakeProvider(name=Provider.GEMINI)
    gemini.register(_Answer, _Answer(text="fine"))
    client = _client(settings, gemini=gemini)
    client.tripped_providers = {Provider.DEEPSEEK}

    result = await client.parse(_Answer, task_type=TaskType.ANALYSIS, system="s", prompt="p")
    assert result.text == "fine"


def test_an_exhausted_repair_budget_is_a_real_attempt_not_a_block() -> None:
    """The same infinite-loop bug in its third form. `step_limit`/`cost_cap` used
    to sit in BLOCKED_REASONS alongside genuinely transient failures, so a repair
    that ran and spent its whole budget was deferred instead of counted -- and the
    next attempt started from the same rolled-back workspace with the same budget
    and reached the same place. One real patch hit 43 attempts over nine hours
    that way, never escalating because nothing ever "failed"."""
    from factory.engines.development.repair import BLOCKED_REASONS, EXHAUSTED_REASONS

    assert EXHAUSTED_REASONS & BLOCKED_REASONS == set(), "a reason cannot be both"
    # Ran and used up its budget -> a real failed attempt, consumes a heal cycle.
    assert {"step_limit", "cost_cap", "deadline"} <= EXHAUSTED_REASONS
    # Never got to run at all -> defer, do not burn a heal cycle.
    assert {"no_provider", "provider_error"} <= BLOCKED_REASONS


def test_a_guided_repair_gets_a_bigger_step_budget_than_a_test_failure() -> None:
    """A test_failure arrives with a traceback naming the file; a guided repair
    arrives as a symptom observed from outside, and finding it costs several reads
    before any edit. Confirmed live: a real 422-on-checkout repair never modified
    the workspace once, hitting step_limit and rolling back every time."""
    from factory.agents.roles import RoleName, get_role
    from factory.engines.development.repair import GUIDED_MAX_STEPS

    assert GUIDED_MAX_STEPS > get_role(RoleName.WORKSPACE_SURGEON).max_steps


def test_circuit_open_is_a_provider_unavailable_subclass() -> None:
    """Load-bearing: every existing `except LLMError` degradation path and
    `except ProviderUnavailable` failover path must keep working unchanged. Only
    the callers that should defer rather than escalate catch the narrower type."""
    from factory.llm.providers.base import LLMError, ProviderUnavailable

    assert issubclass(ProviderCircuitOpen, ProviderUnavailable)
    assert issubclass(ProviderCircuitOpen, LLMError)
