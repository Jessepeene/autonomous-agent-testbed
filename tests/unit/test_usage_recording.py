"""Every LLM call must be attributable to the task that caused it.

Before this fix, `llm_calls.task_id` existed as a column but no call site ever
passed it, so "cost per task" was structurally uncomputable — the exact metric the
`reduce-llm-cost` maintenance item is asked to reason about. Separately,
`AgentRuntime` called providers directly and bypassed `LLMClient` entirely, so an
agent's tokens were never recorded at all, not even without a task_id.
"""

from __future__ import annotations

from factory.agents.runtime import AgentRuntime
from factory.llm.client import LLMClient, UsageRecord
from factory.llm.providers.base import AgentTurn, Provider, Usage
from factory.llm.providers.fake import FakeProvider
from factory.llm.router import ProviderRegistry
from factory.settings import EngineName, Settings, TaskType
from factory.state.schemas import IdeaBatch
from factory.tools.registry import ToolRegistry


class _Recorder:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def __call__(self, record: UsageRecord) -> None:
        self.records.append(record)


async def test_parse_records_the_callers_task_id(
    settings: Settings, gemini_fake: FakeProvider
) -> None:
    gemini_fake.register(IdeaBatch, IdeaBatch(ideas=[]))
    recorder = _Recorder()
    client = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini_fake}),
        settings=settings,
        usage_recorder=recorder,
    )

    await client.parse(
        IdeaBatch, task_type=TaskType.DISCOVERY, system="s", prompt="p", task_id="task-123"
    )

    assert len(recorder.records) == 1
    assert recorder.records[0].task_id == "task-123"


async def test_converse_now_records_usage_at_all(
    settings: Settings, gemini_fake: FakeProvider
) -> None:
    """Regression: agent runs previously called `provider.converse()` directly and no
    row was ever written for them, regardless of task_id."""
    gemini_fake.register_turns(
        AgentTurn(
            text="done",
            tool_calls=[],
            stop_reason="end_turn",
            provider=Provider.GEMINI,
            model="gemini-2.5-flash",
            usage=Usage(input_tokens=10, output_tokens=5),
        )
    )
    recorder = _Recorder()
    client = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini_fake}),
        settings=settings,
        usage_recorder=recorder,
    )

    runtime = AgentRuntime(client, settings=settings, tools=ToolRegistry())
    result = await runtime.run(
        objective="do a thing",
        system="s",
        tool_categories=[],
        task_type=TaskType.ANALYSIS,
        engine=EngineName.SUPERVISOR,
        task_id="agent-task-9",
    )

    assert result.succeeded
    assert len(recorder.records) == 1
    assert recorder.records[0].task_id == "agent-task-9"
    assert recorder.records[0].input_tokens == 10


async def test_an_engine_call_site_threads_task_id_end_to_end(
    settings: Settings, gemini_fake: FakeProvider
) -> None:
    """One representative engine path, proving the plumbing reaches all the way from
    a `run(ctx, session, task)` runner down to the recorded row, not just that
    `LLMClient` itself forwards the field when a test hands it one directly."""
    from factory.engines.discovery.cluster import Cluster
    from factory.engines.discovery.score import generate_ideas
    from factory.engines.discovery.sources.base import Signal
    from factory.hitl.telegram import NullNotifier
    from factory.sandbox.disabled import DisabledSandbox
    from factory.supervisor.context import FactoryContext

    gemini_fake.register(IdeaBatch, IdeaBatch(ideas=[]))
    recorder = _Recorder()
    client = LLMClient(
        ProviderRegistry({Provider.GEMINI: gemini_fake}),
        settings=settings,
        usage_recorder=recorder,
    )
    ctx = FactoryContext(
        settings=settings,
        llm=client,
        notifier=NullNotifier(),
        sandbox=DisabledSandbox(),
        enabled_engines={EngineName.DISCOVERY},
        worker_id="test-worker",
    )
    signal = Signal(
        source="hackernews",
        title="Bookkeepers hate manual reconciliation",
        body="every week I lose hours matching invoices by hand",
        url="https://example.test/1",
        engagement=10,
    )
    cluster = Cluster(signals=[signal])

    await generate_ideas(ctx, [cluster], task_id="discovery-task-7")

    assert len(recorder.records) == 1
    assert recorder.records[0].task_id == "discovery-task-7"
