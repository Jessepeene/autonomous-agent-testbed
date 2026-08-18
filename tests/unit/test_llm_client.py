"""LLM client retry/failover/cost-accounting regression coverage.

All from the 2026-08-10 full-codebase audit:

* A permanent 4xx (bad key, bad schema, model not found) was previously
  misclassified as retryable, burning 3 same-provider retries before silently
  failing over to another vendor with no alarm.
* Any Gemini-routed failover to Anthropic used `model_code` (Opus 5, priced for
  code-quality work) regardless of task type -- every orchestration tick (which
  runs every 15s) would silently run on Opus-5 pricing for as long as Gemini stayed
  down.
* Failed calls (refusal, schema violation) were recorded with `usage=None` even
  when the provider's response carried real, billed token counts -- the cost
  ledger and daily spend cap were both blind to some of the most expensive
  failure modes (e.g. a full adaptive-thinking budget consumed with no answer).
* An intermediate schema-violation attempt that succeeded on retry was never
  recorded at all, even though it was a real, billed call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from factory.llm.client import LLMClient, UsageRecord, _tools_payload
from factory.llm.providers.base import (
    Provider,
    ProviderRefusal,
    ProviderRequestRejected,
    ProviderUnavailable,
    SchemaViolation,
    Usage,
)
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.settings import Settings, TaskType
from factory.state.schemas import IdeaBatch
from factory.tools.registry import ToolDescriptor


@dataclass
class _Recorder:
    records: list[UsageRecord] = field(default_factory=list)

    async def __call__(self, record: UsageRecord) -> None:
        self.records.append(record)


def _client(gemini: FakeProvider, anthropic: FakeProvider, recorder: _Recorder) -> LLMClient:
    settings = Settings(gemini_api_key="test-key", anthropic_api_key="test-key")
    registry = ProviderRegistry({Provider.GEMINI: gemini, Provider.ANTHROPIC: anthropic})
    return LLMClient(registry, settings=settings, usage_recorder=recorder, backoff_base=0.0)


async def test_permanent_rejection_fails_over_immediately_without_retrying() -> None:
    gemini = FakeProvider(Provider.GEMINI)
    gemini.register(
        IdeaBatch,
        ProviderRequestRejected(Provider.GEMINI, "400 bad schema keyword", status=400),
    )
    anthropic = FakeProvider(Provider.ANTHROPIC)
    anthropic.register(IdeaBatch, IdeaBatch(ideas=[]))
    recorder = _Recorder()
    client = _client(gemini, anthropic, recorder)

    await client.parse(IdeaBatch, task_type=TaskType.ANALYSIS, system="s", prompt="p")

    # Exactly one attempt against the rejecting provider, not the usual 3.
    assert len(gemini.calls) == 1
    assert len(anthropic.calls) == 1
    outcomes = [r.outcome for r in recorder.records]
    assert outcomes == ["rejected", "ok"]


async def test_transient_unavailability_still_retries_before_failing_over() -> None:
    """Regression guard: the fast-path for permanent errors must not weaken the
    existing retry behaviour for genuinely transient ones."""
    gemini = FakeProvider(Provider.GEMINI)
    gemini.register(
        IdeaBatch,
        ProviderUnavailable(Provider.GEMINI, "503 unavailable", status=503),
        ProviderUnavailable(Provider.GEMINI, "503 unavailable", status=503),
        IdeaBatch(ideas=[]),
    )
    anthropic = FakeProvider(Provider.ANTHROPIC)
    recorder = _Recorder()
    client = _client(gemini, anthropic, recorder)

    await client.parse(IdeaBatch, task_type=TaskType.ANALYSIS, system="s", prompt="p")

    assert len(gemini.calls) == 3
    assert len(anthropic.calls) == 0
    assert recorder.records[-1].outcome == "ok"


async def test_failover_to_anthropic_uses_the_cheap_fallback_model_not_model_code() -> None:
    gemini = FakeProvider(Provider.GEMINI)
    gemini.register(IdeaBatch, ProviderRequestRejected(Provider.GEMINI, "400", status=400))
    anthropic = FakeProvider(Provider.ANTHROPIC)
    anthropic.register(IdeaBatch, IdeaBatch(ideas=[]))
    recorder = _Recorder()
    client = _client(gemini, anthropic, recorder)

    await client.parse(IdeaBatch, task_type=TaskType.ORCHESTRATION, system="s", prompt="p")

    assert anthropic.calls[0]["model"] == "claude-haiku-4-5"
    assert anthropic.calls[0]["model"] != "claude-opus-5"


async def test_refusal_records_real_usage_not_zero() -> None:
    gemini = FakeProvider(Provider.GEMINI)
    gemini.register(
        IdeaBatch,
        ProviderRefusal(
            Provider.GEMINI, "SAFETY", "blocked", usage=Usage(input_tokens=120, output_tokens=0)
        ),
    )
    anthropic = FakeProvider(Provider.ANTHROPIC)
    recorder = _Recorder()
    client = _client(gemini, anthropic, recorder)

    with pytest.raises(ProviderRefusal):
        await client.parse(IdeaBatch, task_type=TaskType.ANALYSIS, system="s", prompt="p")

    assert len(recorder.records) == 1
    assert recorder.records[0].outcome == "refusal"
    assert recorder.records[0].input_tokens == 120


async def test_schema_violation_repair_records_the_failed_attempt_too() -> None:
    gemini = FakeProvider(Provider.GEMINI)
    gemini.register(
        IdeaBatch,
        SchemaViolation("bad json", usage=Usage(input_tokens=50, output_tokens=200)),
        IdeaBatch(ideas=[]),
    )
    anthropic = FakeProvider(Provider.ANTHROPIC)
    recorder = _Recorder()
    client = _client(gemini, anthropic, recorder)

    await client.parse(IdeaBatch, task_type=TaskType.ANALYSIS, system="s", prompt="p")

    outcomes = [r.outcome for r in recorder.records]
    assert outcomes == ["schema_violation_repaired", "ok"]
    # The repaired attempt's own billed usage was not silently dropped.
    assert recorder.records[0].input_tokens == 50
    assert recorder.records[0].output_tokens == 200


async def test_schema_violation_that_never_repairs_is_still_recorded_with_usage() -> None:
    gemini = FakeProvider(Provider.GEMINI)
    gemini.register(
        IdeaBatch,
        SchemaViolation("bad json 1", usage=Usage(input_tokens=10, output_tokens=10)),
        SchemaViolation("bad json 2", usage=Usage(input_tokens=10, output_tokens=64000)),
    )
    anthropic = FakeProvider(Provider.ANTHROPIC)
    recorder = _Recorder()
    client = _client(gemini, anthropic, recorder)

    with pytest.raises(SchemaViolation):
        await client.parse(IdeaBatch, task_type=TaskType.ANALYSIS, system="s", prompt="p")

    outcomes = [r.outcome for r in recorder.records]
    assert outcomes == ["schema_violation_repaired", "schema_violation"]
    assert recorder.records[1].output_tokens == 64000


async def test_deepseek_is_never_an_accidental_failover_target() -> None:
    """Real, reproducible regression: DeepSeek is a dedicated single-purpose
    provider (CODE_REPAIR only, itself in NO_FAILOVER), but ProviderRegistry.
    alternatives() has no concept of task-type appropriateness -- with three
    providers registered, a Gemini-routed ANALYSIS call failing over used to
    treat DeepSeek as a candidate too, sending it a model name it has never
    heard of. Confirmed live: one real ANALYSIS call reached DeepSeek this way
    and was rejected for an unrecognised model before this fix."""
    gemini = FakeProvider(Provider.GEMINI)
    gemini.register(IdeaBatch, ProviderRequestRejected(Provider.GEMINI, "400", status=400))
    anthropic = FakeProvider(Provider.ANTHROPIC)
    anthropic.register(IdeaBatch, IdeaBatch(ideas=[]))
    deepseek = FakeProvider(Provider.DEEPSEEK)
    settings = Settings(gemini_api_key="test-key", anthropic_api_key="test-key")
    registry = ProviderRegistry(
        {Provider.GEMINI: gemini, Provider.ANTHROPIC: anthropic, Provider.DEEPSEEK: deepseek}
    )
    client = LLMClient(registry, settings=settings, backoff_base=0.0)

    await client.parse(IdeaBatch, task_type=TaskType.ANALYSIS, system="s", prompt="p")

    assert len(deepseek.calls) == 0
    assert len(anthropic.calls) == 1


def test_deepseek_gets_anthropics_tool_schema_shape_not_geminis() -> None:
    """Real, reproducible regression: DeepSeek is served over
    DeepSeekProvider(AnthropicProvider) -- the same Anthropic-compatible
    endpoint/message format -- but defaulted to Gemini's tool-schema shape here
    (only Provider.ANTHROPIC was special-cased), which DeepSeek's endpoint
    rejected outright the first time CODE_REPAIR ran against real production
    traffic: 400 "Invalid schema for function 'list_workspace_files': null is
    not of types 'boolean', 'object'"."""
    async def _handler() -> None:
        return None

    descriptors = [
        ToolDescriptor(
            name="list_workspace_files",
            description="d",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            category="workspace",
            handler=_handler,
        )
    ]

    anthropic_shaped = _tools_payload(descriptors, Provider.ANTHROPIC)
    deepseek_shaped = _tools_payload(descriptors, Provider.DEEPSEEK)
    gemini_shaped = _tools_payload(descriptors, Provider.GEMINI)

    assert deepseek_shaped == anthropic_shaped
    assert "input_schema" in deepseek_shaped[0]
    assert "parameters" not in deepseek_shaped[0]
    assert "parameters" in gemini_shaped[0]
