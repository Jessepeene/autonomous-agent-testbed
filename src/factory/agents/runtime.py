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
import re
import time
from dataclasses import dataclass, field
from typing import Any

from factory.llm.client import LLMClient, NoProviderAvailable
from factory.llm.providers.base import LLMError, ProviderRefusal, ToolResult, Turn, Usage
from factory.logging import get_logger
from factory.settings import EngineName, Settings, TaskType, get_settings
from factory.tools.registry import REGISTRY, ToolDescriptor, ToolRegistry

log = get_logger(__name__)

_TOOL_TIMEOUT_SECONDS = 120

#: Invocations recorded in the `agent.finished` log line, and the per-argument
#: ceiling. A repair agent can make dozens of calls and a single argument can carry
#: an entire file's contents, so both are bounded -- this is a durable log on a disk
#: that has run low, not a debugger.
_MAX_LOGGED_INVOCATIONS = 40
_MAX_LOGGED_ARG_CHARS = 300


#: The closed vocabulary `_REPAIR_RULES` mandates. Matched case-insensitively and
#: anywhere in the final message rather than only as the last line: the rule says
#: "end your final message with one line, exactly" and models approximate that
#: instruction more often than they follow it, so a strict match would silently
#: discard most real reports -- which is the failure this whole channel exists to
#: end.
_RESULT_SENTINEL = re.compile(
    r"RESULT:\s*(FIXED|NOT_FIXED|BLOCKED)\b[ \t]*(?P<detail>[^\n]*)", re.IGNORECASE
)


def parse_reported_result(answer: str) -> tuple[str, str]:
    """``(verdict, detail)`` from an agent's own final message, or ``("", "")``.

    The last occurrence wins: an agent that narrates its plan ("I will end with
    RESULT: FIXED once the tests pass") and then reports for real should be taken
    at its final word, not its first.
    """
    matches = list(_RESULT_SENTINEL.finditer(answer or ""))
    if not matches:
        return "", ""
    last = matches[-1]
    return last.group(1).upper(), last.group("detail").strip()[:500]


def _summarise_arguments(arguments: dict[str, Any]) -> dict[str, str]:
    """Tool arguments, truncated, for the run's audit line.

    Values only -- no filtering by key. Nothing an agent passes to a tool in this
    codebase is a credential: the tool registry withholds a tool entirely when its
    `required_credential` is absent (`ToolRegistry.usable`), so secrets reach
    providers through settings, never through a model-supplied argument.
    """
    summarised = {}
    for key, value in arguments.items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        summarised[key] = (
            text[:_MAX_LOGGED_ARG_CHARS] + "..." if len(text) > _MAX_LOGGED_ARG_CHARS else text
        )
    return summarised


_MAX_RESULT_CHARS = 20_000

#: Marker inserted where `_truncate_middle` removed content, so the model can tell a
#: dropped middle from a genuinely short result and does not reason about the gap as
#: if it were absence.
_ELISION = "\n...[{dropped} characters elided]...\n"

#: Same honesty principle as `_ELISION`, at turn granularity: `_truncate_middle`
#: shortens the middle of one oversized result, this replaces the *entire* content
#: of an old tool-result turn once the whole transcript is over budget. The model
#: sees plainly that history was dropped for space, not that the tool suddenly
#: returned nothing.
_TURN_ELISION = "[{count} earlier tool result(s) elided to stay under the context budget]"


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


def _turn_chars(turn: Turn) -> int:
    """Character weight of one turn -- chars, not tokens, matching the existing
    convention (`max_result_chars` is already char-based, not token-based). Tool
    results are the bulk of a transcript (`_mark_conversation_prefix`'s own
    docstring in `anthropic_provider.py`), so they dominate this count; tool-call
    arguments are counted too since they are real content the model re-reads every
    step, just usually small structured JSON next to a tool result's payload."""
    calls_chars = sum(len(json.dumps(c.arguments, default=str)) for c in turn.tool_calls)
    results_chars = sum(len(r.content) for r in turn.tool_results)
    return len(turn.text) + calls_chars + results_chars


def _context_chars(turns: list[Turn]) -> int:
    return sum(_turn_chars(t) for t in turns)


def _compact_to_budget(turns: list[Turn], max_context_chars: int) -> None:
    """Drop the oldest tool-result turns' content, oldest first, until back under
    budget or nothing more can be dropped. Mutates ``turns`` in place.

    Never touches ``turns[0]`` (the objective -- and any recalled memory/context
    joined into it, see `spawner.py::spawn`) or the last two turns (the model's own
    most recent action and its result), so a compacted run never loses its
    objective and the model is never asked to continue from a state it cannot see.
    Only entire tool-result turns are dropped, never the interleaved assistant
    turns -- an assistant turn with no matching tool_result would desync the
    conversation for a provider that expects every tool_use answered.
    """
    protected_head = 1
    protected_tail = 2
    for i in range(protected_head, len(turns) - protected_tail):
        if _context_chars(turns) <= max_context_chars:
            return
        turn = turns[i]
        if not turn.tool_results:
            continue
        marker = _TURN_ELISION.format(count=len(turn.tool_results))
        turns[i] = Turn(
            role=turn.role,
            tool_results=[
                ToolResult(call_id=r.call_id, name=r.name, content=marker, is_error=r.is_error)
                for r in turn.tool_results
            ],
        )


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
    #: High-water mark of `_context_chars(turns)` across the run, recorded every
    #: step regardless of whether `max_context_chars` was ever set or exceeded --
    #: instrumentation for tuning a role's budget, not just a compaction artifact.
    peak_context_chars: int = 0
    #: What the agent itself said it achieved: FIXED / NOT_FIXED / BLOCKED, or ""
    #: when it did not report.
    #:
    #: This is a channel that already existed and nothing read. `_REPAIR_RULES`
    #: (agents/roles.py) has always instructed every repair agent to end with
    #: `RESULT: <verdict>` plus the command it ran and its exit code, and
    #: `_SHARED_RULES` tells every other role to "say so plainly and state exactly
    #: what is missing" when it cannot proceed. No code anywhere parsed either, so
    #: agents reported blocked status into the void while the decision was made
    #: from file diffs. A repair agent spent three separate runs discovering its
    #: toolchain had been deleted, said so, and was recorded as having simply
    #: failed.
    #:
    #: Deliberately advisory, not authoritative. `stopped_because` (set by the
    #: runtime) and the file diff (checked by `repair.audit_edits`) remain the
    #: source of truth for *what happened*; this is the agent's account of *why*,
    #: which is the part nothing else can supply.
    reported_result: str = ""
    reported_detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.stopped_because == "completed"

    @property
    def reported_blocked(self) -> bool:
        """The agent said it could not proceed for a reason outside its control."""
        return self.reported_result == "BLOCKED"

    @property
    def used_tools(self) -> bool:
        """Whether the agent touched anything real at all. `succeeded` means
        only "the model emitted a final turn with no tool calls and non-empty
        text" -- a confident, fully hallucinated answer with zero tool calls
        satisfies it exactly as well as one backed by real tool use. Tier 1
        finding #1/#2/#16 of the system-wide verification plan: this is the
        generalization of the one check `development/repair.py::attempt_repair`
        already made (`if not result.invocations or not changed`) into shared
        infrastructure, so every other `spawn()` caller can apply the same
        "evidence, not self-report" discipline instead of trusting `succeeded`
        alone.
        """
        return bool(self.invocations)

    def used_tool(self, *names: str) -> bool:
        """Whether at least one of `names` was actually, successfully called --
        stronger than `used_tools` for a caller that only trusts the answer if
        a *specific* tool backs it (e.g. entity resolution only trusts an
        "I investigated" claim if `http` was actually invoked)."""
        return any(inv.name in names and inv.ok for inv in self.invocations)

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
        max_context_chars: int | None = None,
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
            # Tracked every step regardless of whether a budget is set, so the
            # instrumentation is useful even for roles that never hit it.
            context_chars = _context_chars(turns)
            result.peak_context_chars = max(result.peak_context_chars, context_chars)
            if max_context_chars is not None and context_chars > max_context_chars:
                # Compact deterministically first -- an agent that would have hit
                # the window and returned a truncated answer now continues, instead
                # of stopping the instant it saturates. Only a genuine "nothing left
                # to drop and still over budget" (a pathologically large objective
                # or a single oversized turn) actually ends the run.
                _compact_to_budget(turns, max_context_chars)
                context_chars = _context_chars(turns)
                if context_chars > max_context_chars:
                    result.stopped_because = "context_budget"
                    log.warning(
                        "agent.context_budget",
                        step=step,
                        budget_chars=max_context_chars,
                        context_chars=context_chars,
                    )
                    break
                log.info("agent.context_compacted", step=step, context_chars_after=context_chars)
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
                # Re-parsed on every text turn, not only the last: a run that
                # reports and is then cut off by its step ceiling still has its
                # account captured, which is exactly the case that was being lost.
                verdict, detail = parse_reported_result(turn.text)
                if verdict:
                    result.reported_result, result.reported_detail = verdict, detail

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
            # The arguments, not just the tool names. A run that reports
            # `run_workspace_command x20` is unreadable without them: it could be
            # twenty attempts at one failing test, or twenty different diagnostics,
            # and those call for opposite fixes. Diagnosing a real repair failure
            # stalled here -- the name list showed the agent never wrote a file, and
            # nothing recorded what it did instead.
            #
            # Bounded hard: this goes to a durable log on a disk that has run low,
            # and a tool argument can carry a whole file's contents.
            invocations=[
                {
                    "tool": inv.name,
                    "ok": inv.ok,
                    "args": _summarise_arguments(inv.arguments),
                    **({} if inv.ok else {"detail": inv.detail[:200]}),
                }
                for inv in result.invocations[:_MAX_LOGGED_INVOCATIONS]
            ],
        )
        return result

    async def _execute(
        self,
        call: Any,
        by_name: dict[str, ToolDescriptor],
        *,
        max_result_chars: int = _MAX_RESULT_CHARS,
    ) -> tuple[ToolInvocation, Any]:
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
