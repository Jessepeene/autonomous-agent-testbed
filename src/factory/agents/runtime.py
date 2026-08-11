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

#: Marker inserted where `_truncate_middle` removed content, so the model can tell a
#: dropped middle from a genuinely short result and does not reason about the gap as
#: if it were absence.
_ELISION = "\n...[{dropped} characters elided]...\n"


def _truncate_middle(content: str, limit: int) -> str:
    """Keep both ends of an oversized tool result, dropping the middle.

    Head-only truncation was actively harmful here. A tool result is JSON with the
    keys in insertion order, and `run_in_sandbox` returns
    ``exit_code, stdout, stderr, timed_out`` -- so a chatty pytest run (collection
    output, hundreds of passing dots) filled the budget with noise and cut `stderr`,
    the actual traceback, off the end entirely. `_verify_and_ship` has always taken
    ``[-2000:]`` of test output for exactly this reason: pytest puts the failure
    summary at the *end*.

    Keeping both ends preserves the JSON preamble (exit code, how the command
    started) and the tail (what actually failed), which is what a repair agent needs
    to diagnose rather than guess.
    """
    if len(content) <= limit:
        return content
    marker = _ELISION.format(dropped=len(content) - limit)
    budget = max(limit - len(marker), 0)
    # Tail-weighted: the end of a failing command's output carries the diagnosis,
    # the beginning carries context. 40/60 keeps enough of both.
    head = budget * 2 // 5
    tail = budget - head
    return f"{content[:head]}{marker}{content[-tail:]}" if tail else content[:head]


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
        max_result_chars: int = _MAX_RESULT_CHARS,
        cost_cap_usd: float | None = None,
        cost_model: str | None = None,
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
            # Checked here, at the top of a step, rather than mid-tool-round: the last
            # thing that happened is then always a complete tool round, so the caller
            # inherits a coherent state rather than one with a tool call issued and
            # its result never folded in. `max_steps` alone is a poor proxy for spend
            # -- an agent re-sends its whole transcript every step, so a run with big
            # tool results can cost several times another run of the same length.
            if cost_cap_usd is not None and _spent_usd(result.usage, cost_model) >= cost_cap_usd:
                result.stopped_because = "cost_cap"
                log.warning(
                    "agent.cost_cap",
                    step=step,
                    cap_usd=cost_cap_usd,
                    spent_usd=round(_spent_usd(result.usage, cost_model), 4),
                )
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
            deadline_hit_mid_step = False
            for call in turn.tool_calls:
                # Previously only checked once per step, before the LLM call above --
                # a single turn can request several tool calls, each executed
                # serially and each individually allowed up to _TOOL_TIMEOUT_SECONDS
                # (120s). A turn with e.g. 5 slow calls could run ~600s past the
                # point the deadline should have fired, undetected until the *next*
                # step began.
                if time.monotonic() - started > deadline_seconds:
                    deadline_hit_mid_step = True
                    break
                invocation, tool_result = await self._execute(
                    call, by_name, max_result_chars=max_result_chars
                )
                result.invocations.append(invocation)
                tool_results.append(tool_result)
            if deadline_hit_mid_step:
                result.stopped_because = "deadline"
                break
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
        self,
        call: Any,
        by_name: dict[str, ToolDescriptor],
        *,
        max_result_chars: int = _MAX_RESULT_CHARS,
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
            content = _truncate_middle(content, max_result_chars)
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


def _spent_usd(usage: Usage, model: str | None) -> float:
    """Cost of an in-flight run so far.

    ``model`` is passed in rather than inferred because the runtime does not know
    which provider served a step. For the one task type that uses a cost cap today
    (CODE_GENERATION) that is deterministic -- it is in ``NO_FAILOVER``, so it can
    only ever have been served by the configured code model -- and ``estimate_usd``
    falls back to a mid-range default price for an unknown name, which errs toward
    stopping early rather than overspending.
    """
    from factory.observability.costs import estimate_usd

    return estimate_usd(
        model or "",
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
    )


def _accumulate(total: Usage, step: Usage) -> None:
    total.input_tokens += step.input_tokens
    total.output_tokens += step.output_tokens
    total.cache_read_tokens += step.cache_read_tokens
    total.cache_write_tokens += step.cache_write_tokens
    total.latency_ms += step.latency_ms
