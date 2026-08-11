"""The agent runtime — the loop that actually executes tools.

Until this module existed the MCP registry was a catalogue nothing read from: tools
were declared, filtered by credential and sandbox availability, rendered into a
provider payload, and then never invoked. This closes that loop.

Every guardrail an ephemeral agent could otherwise walk past is enforced *here*,
because the agent chooses what to call and the runtime decides whether it may:

* **Tool allowlist** — an agent only ever sees the categories it was spawned with, so
  a research agent cannot reach the sandbox and a maintenance agent cannot send email.
* **Step ceiling** — an agent that loops calling the same tool stops, rather than
  burning tokens until someone notices.
* **Wall-clock deadline** — a hung tool cannot pin a worker forever.
* **Unknown tool names fail as tool results, not exceptions** — a hallucinated tool
  name is something the model should see and correct, not a crash that loses the run.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from factory.llm.client import LLMClient, NoProviderAvailable
from factory.llm.providers.base import LLMError, ProviderRefusal, Turn, Usage
from factory.logging import get_logger
from factory.settings import EngineName, Settings, TaskType, get_settings
from factory.tools.registry import REGISTRY, ToolDescriptor, ToolRegistry

log = get_logger(__name__)

_TOOL_TIMEOUT_SECONDS = 120
_MAX_RESULT_CHARS = 20_000


@dataclass(slots=True)
class ToolInvocation:
    name: str
    arguments: dict[str, Any]
    ok: bool
    detail: str


@dataclass(slots=True)
class AgentResult:
    objective: str
    answer: str
    steps: int
    invocations: list[ToolInvocation] = field(default_factory=list)
    stopped_because: str = "completed"
    usage: Usage = field(default_factory=Usage)

    @property
    def succeeded(self) -> bool:
        return self.stopped_because == "completed"

    def summary(self) -> str:
        tools = ", ".join(sorted({i.name for i in self.invocations})) or "none"
        return f"{self.steps} step(s), tools used: {tools}, stopped: {self.stopped_because}"


class AgentRuntime:
    """Runs one ephemeral agent to completion."""

    def __init__(
        self,
        llm: LLMClient,
        *,
        settings: Settings | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings or get_settings()
        self._tools = tools or REGISTRY

    def available_tools(
        self, categories: list[str], *, docker_available: bool
    ) -> list[ToolDescriptor]:
        return self._tools.get_mcp_tools(
            category=categories,
            settings=self._settings,
            docker_available=docker_available,
        )

    async def run(
        self,
        *,
        objective: str,
        system: str,
        tool_categories: list[str],
        task_type: TaskType = TaskType.ANALYSIS,
        engine: EngineName | None = None,
        task_id: str | None = None,
        max_steps: int = 8,
        deadline_seconds: int = 600,
        docker_available: bool = False,
        context: str = "",
    ) -> AgentResult:
        descriptors = self.available_tools(tool_categories, docker_available=docker_available)
        by_name = {d.name: d for d in descriptors}

        prompt = f"{objective}\n\n{context}".strip()
        turns: list[Turn] = [Turn(role="user", text=prompt)]

        result = AgentResult(objective=objective, answer="", steps=0)
        started = time.monotonic()

        for step in range(1, max_steps + 1):
            if time.monotonic() - started > deadline_seconds:
                result.stopped_because = "deadline"
                break
            result.steps = step

            try:
                turn = await self._llm.converse(
                    system=system,
                    turns=turns,
                    tool_descriptors=descriptors,
                    task_type=task_type,
                    engine=engine,
                    task_id=task_id,
                )
            except ProviderRefusal:
                # A policy decline is a decision; the caller escalates rather than
                # retrying or quietly returning a half-answer.
                raise
            except NoProviderAvailable:
                # Not a step in any real sense — no call was ever made.
                result.steps = 0
                result.stopped_because = "no_provider"
                break
            except LLMError as exc:
                log.warning("agent.turn_failed", detail=str(exc)[:300], step=step)
                result.stopped_because = "provider_error"
                break

            _accumulate(result.usage, turn.usage)
            if turn.text:
                result.answer = turn.text

            if not turn.wants_tools:
                # stop_reason was previously never consulted here: any turn with no
                # tool calls was "completed", including an empty answer (adaptive
                # thinking consuming the whole output budget is a real, observed
                # failure mode -- see anthropic_provider.py's SchemaViolation handling
                # for the identical case in parse()) or text truncated mid-thought by
                # hitting the token budget. Both used to read as success: a
                # maintenance cycle that produced nothing logged "done", and a
                # truncated half-sentence got written into permanent memory as a
                # LESSON (spawner.py gates that write on result.succeeded, so fixing
                # the signal here is enough -- no change needed there).
                if not turn.text.strip():
                    result.stopped_because = "empty_response"
                elif "max_token" in turn.stop_reason.lower():
                    result.stopped_because = "truncated"
                else:
                    result.stopped_because = "completed"
                break

            turns.append(Turn(role="assistant", text=turn.text, tool_calls=turn.tool_calls))
            tool_results = []
            for call in turn.tool_calls:
                invocation, tool_result = await self._execute(call, by_name)
                result.invocations.append(invocation)
                tool_results.append(tool_result)
            turns.append(Turn(role="user", tool_results=tool_results))
        else:
            result.stopped_because = "step_limit"

        log.info(
            "agent.finished",
            engine=str(engine) if engine else None,
            steps=result.steps,
            stopped=result.stopped_because,
            tools=[i.name for i in result.invocations],
        )
        return result

    async def _execute(
        self, call: Any, by_name: dict[str, ToolDescriptor]
    ) -> tuple[ToolInvocation, Any]:
        from factory.llm.providers.base import ToolResult

        descriptor = by_name.get(call.name)
        if descriptor is None:
            # The model asked for something it was not offered. Tell it so, rather
            # than raising — it can pick a real tool on the next step.
            detail = (
                f"No tool named {call.name!r} is available to you. "
                f"Available tools: {', '.join(sorted(by_name)) or 'none'}."
            )
            return (
                ToolInvocation(call.name, call.arguments, ok=False, detail=detail),
                ToolResult(call_id=call.id, name=call.name, content=detail, is_error=True),
            )

        try:
            raw = await asyncio.wait_for(
                descriptor.handler(**call.arguments), timeout=_TOOL_TIMEOUT_SECONDS
            )
            content = raw if isinstance(raw, str) else json.dumps(raw, default=str)
            content = content[:_MAX_RESULT_CHARS]
            return (
                ToolInvocation(call.name, call.arguments, ok=True, detail="ok"),
                ToolResult(call_id=call.id, name=call.name, content=content),
            )
        except TimeoutError:
            detail = f"{call.name} exceeded {_TOOL_TIMEOUT_SECONDS}s and was cancelled"
            log.warning("agent.tool_timeout", tool=call.name)
        except TypeError as exc:
            detail = f"{call.name} rejected those arguments: {exc}"
        except Exception as exc:  # a failing tool is a result, not a crash
            detail = f"{call.name} failed: {exc}"
            log.warning("agent.tool_failed", tool=call.name, detail=str(exc)[:200])

        return (
            ToolInvocation(call.name, call.arguments, ok=False, detail=detail),
            ToolResult(call_id=call.id, name=call.name, content=detail, is_error=True),
        )


def _accumulate(total: Usage, step: Usage) -> None:
    total.input_tokens += step.input_tokens
    total.output_tokens += step.output_tokens
    total.cache_read_tokens += step.cache_read_tokens
    total.cache_write_tokens += step.cache_write_tokens
    total.latency_ms += step.latency_ms
