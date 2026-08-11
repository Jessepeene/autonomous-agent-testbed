"""Ephemeral agent roles.

A role is a capability envelope, not a personality: a system prompt plus the tool
categories that role is permitted to touch plus a step ceiling. The Orchestrator
picks a role by name; it does not get to invent one at runtime.

That constraint is deliberate. Letting a model compose its own tool set would mean
the thing being contained decides what it is contained by — a research agent that
wanted the sandbox could simply ask for it. Roles are the allowlist, and adding one
is a code change that a human reviews.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from factory.settings import TaskType


class RoleName(StrEnum):
    RESEARCHER = "researcher"
    ANALYST = "analyst"
    REPAIR_ENGINEER = "repair_engineer"
    AUDITOR = "auditor"
    LEAD_SCOUT = "lead_scout"
    #: Excluded from `role_catalogue()` -- see WORKSPACE_SCOPED_ROLES.
    WORKSPACE_SURGEON = "workspace_surgeon"


#: Roles the Orchestrator must not be able to pick. A workspace surgeon is only
#: correct inside `development/graph.py::_patch`, which pins it to one product, holds
#: `product_lock` for its whole run, snapshots the workspace so its edits can be
#: rolled back, and audits what it changed afterwards. Spawned from the Orchestrator
#: it would have none of that: no lock, no snapshot, no heal accounting, and a
#: model-chosen objective. `role_catalogue()` filters these out so the Orchestrator's
#: own prompt never lists them as an option.
WORKSPACE_SCOPED_ROLES = frozenset({RoleName.WORKSPACE_SURGEON})


@dataclass(frozen=True, slots=True)
class AgentRole:
    name: RoleName
    purpose: str
    system_prompt: str
    tool_categories: tuple[str, ...]
    task_type: TaskType
    max_steps: int = 8
    deadline_seconds: int = 600
    #: Roles that can act on the world (write files, send things) rather than only
    #: read. Used to keep destructive capability out of dry-run and out of roles that
    #: were spawned for a read-only question.
    mutating: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


_SHARED_RULES = """
Operating rules:
- You have a limited number of steps. Spend them on tool calls that change your
  answer, not on confirming things you already know.
- If a tool returns an error, read it and adapt. Do not call the same tool with the
  same arguments twice.
- If you cannot complete the objective with the tools you have, say so plainly and
  state exactly what is missing. A clear "blocked, and here is why" is more useful
  than a confident guess.
- Never invent a URL, a metric, a customer, or a quote. If you did not read it from a
  tool result, do not assert it.
- When you are done, give the answer directly. No preamble.
"""

#: `_SHARED_RULES` with the one line that would break a repair loop removed, plus the
#: constraints specific to editing a workspace that gets reviewed and deployed.
#:
#: The removed line is "Do not call the same tool with the same arguments twice." For
#: every other role that is good advice; for this one it forbids the entire method --
#: a repair agent MUST run the identical test command before and after its edit,
#: because that before/after pair is the only evidence the fix actually worked.
_REPAIR_RULES = """
Operating rules:
- You have a limited number of steps. Spend them on tool calls that change your
  answer, not on confirming things you already know.
- Running the SAME test command again after an edit is expected and required -- that
  is how you verify the fix. Do not skip it to save a step.
- If a tool returns an error, read it and adapt. Do not repeat a call that failed for
  the same reason twice.
- You have no network access. `pip install`, `curl` and `apt` will all fail.
  Dependencies are already installed; the test command sees them.
- Never delete, rename, skip, xfail, or weaken an assertion in any test, and never
  suppress an error to make a test pass. If you believe a test itself is wrong, say
  so and stop -- do not edit it.
- Do not modify requirements.txt unless the failure is an import error naming a
  package that is genuinely missing.
- Every file you leave in the workspace is read by a code reviewer and then deployed.
  No scratch files, no debug scripts, no .bak or .orig copies, and never a second
  copy of a module -- a stale duplicate alongside the real one is a bug that has
  blocked a deploy here before.
- If you cannot fix it with the tools you have, say so plainly and state exactly what
  is missing. A clear "blocked, and here is why" is more useful than a guess.
- Never invent a file, a line number, or an error message. If you did not read it
  from a tool result, do not assert it.
- End your final message with one line, exactly: RESULT: FIXED, RESULT: NOT_FIXED, or
  RESULT: BLOCKED -- followed by the test command you last ran and its exit code.
"""

ROLES: dict[RoleName, AgentRole] = {
    RoleName.RESEARCHER: AgentRole(
        name=RoleName.RESEARCHER,
        purpose="Answer an open question about the outside world using public sources.",
        system_prompt=(
            "You are a research agent for an autonomous B2B software company. You "
            "investigate a specific question using public web sources and report what "
            "you actually found.\n"
            "Prefer primary sources over summaries. Quote the specific claim and give "
            "its URL. Where sources disagree, say so rather than picking one.\n" + _SHARED_RULES
        ),
        tool_categories=("http", "git"),
        task_type=TaskType.ANALYSIS,
        max_steps=10,
    ),
    RoleName.ANALYST: AgentRole(
        name=RoleName.ANALYST,
        purpose="Reason over factory state and produce a decision or recommendation.",
        system_prompt=(
            "You are an analyst for an autonomous B2B software company. You are given "
            "factory state and asked for a judgement.\n"
            "Quantify where you can. Name the single highest-leverage action rather "
            "than listing ten. If the data does not support a conclusion, say which "
            "query or file would settle it.\n" + _SHARED_RULES
        ),
        tool_categories=("http",),
        task_type=TaskType.ANALYSIS,
        max_steps=6,
    ),
    RoleName.REPAIR_ENGINEER: AgentRole(
        name=RoleName.REPAIR_ENGINEER,
        purpose="Diagnose and fix a failing build or a broken live product.",
        system_prompt=(
            "You are a repair engineer for an autonomous software factory. A build or "
            "a live product is failing and you must find out why.\n"
            "Reproduce before you theorise: run the failing command in the sandbox and "
            "read the actual error. Change the smallest thing that fixes the root "
            "cause. Do not refactor surrounding code, and do not suppress an error to "
            "make a test pass.\n" + _SHARED_RULES
        ),
        tool_categories=("sandbox", "git", "http"),
        task_type=TaskType.CODE_GENERATION,
        max_steps=14,
        deadline_seconds=1800,
        mutating=True,
    ),
    RoleName.AUDITOR: AgentRole(
        name=RoleName.AUDITOR,
        purpose="Check a live product's health and report degradation.",
        system_prompt=(
            "You are an auditor for an autonomous software company. You check whether "
            "a deployed product is actually working from the outside.\n"
            "Fetch its health endpoint and its main page. Report concrete observations: "
            "status codes, response times, error text. Distinguish 'degraded' from "
            "'down' from 'fine'. Do not speculate about causes you cannot observe.\n"
            + _SHARED_RULES
        ),
        tool_categories=("http",),
        task_type=TaskType.ANALYSIS,
        max_steps=6,
    ),
    RoleName.WORKSPACE_SURGEON: AgentRole(
        name=RoleName.WORKSPACE_SURGEON,
        purpose="Fix a failing test in one product workspace by editing it in place.",
        system_prompt=(
            "You are repairing one product's codebase. Its test suite is failing and "
            "your job is to make it pass by fixing the underlying bug.\n"
            "Reproduce first: run the test command and read the real failure before "
            "you change anything. Then read the file it points at. Change the "
            "smallest thing that fixes the root cause, and run the test command "
            "again to confirm. Do not refactor code that is not implicated.\n" + _REPAIR_RULES
        ),
        tool_categories=("workspace",),
        task_type=TaskType.CODE_GENERATION,
        # 8, not the repair_engineer's 14: an agent loop re-sends its whole transcript
        # every step, so cost grows superlinearly in step count -- past roughly this
        # many steps a surgical repair stops being cheaper than the full regeneration
        # it replaces, which is the entire reason it exists.
        max_steps=8,
        # Comfortably under `task_lease_seconds` (900) so a run cannot outlive its own
        # task lease and be reclaimed underneath itself.
        deadline_seconds=600,
        # Mutating in the file-writing sense, but deliberately NOT flagged as such:
        # `can_spawn` refuses mutating roles under FACTORY_DRY_RUN, and edits confined
        # to a product workspace are the same blast radius `write_plan()` and
        # `clear_workspace()` already have in dry run today. FACTORY_DRY_RUN exists to
        # stop *outward* effects -- email, payments, real deploys -- all of which
        # remain blocked. Flagging this would make dry run strictly worse than doing
        # nothing: the agent would never run, and every heal cycle would fail
        # identically instead of exercising the pipeline.
        mutating=False,
    ),
    RoleName.LEAD_SCOUT: AgentRole(
        name=RoleName.LEAD_SCOUT,
        purpose="Find prospects for a live product through the compliant lead providers.",
        system_prompt=(
            "You are a lead scout for a B2B software company established in the "
            "Netherlands. You search contact databases for professionals whose role "
            "plausibly needs a specific tool.\n"
            "Search narrowly. A tight list of titles that will actually buy beats a "
            "broad list that generates volume — every additional wrong person is "
            "someone whose data gets processed without a real basis for it.\n"
            "You never see or handle personal contact details: the tools return only "
            "role, company and email domain. Storage and outreach happen elsewhere, "
            "behind a Legitimate Interest Assessment you do not control.\n" + _SHARED_RULES
        ),
        tool_categories=("leads",),
        task_type=TaskType.ANALYSIS,
        max_steps=6,
    ),
}


def get_role(name: RoleName | str) -> AgentRole:
    key = RoleName(name) if not isinstance(name, RoleName) else name
    return ROLES[key]


def role_catalogue() -> str:
    """Rendered for the Orchestrator prompt so it can pick a role by name.

    Workspace-scoped roles are omitted: this catalogue is precisely the menu the
    Orchestrator chooses from, and those roles are only safe inside the caller that
    pins them to a product and can roll their edits back (see WORKSPACE_SCOPED_ROLES).
    """
    return "\n".join(
        f"- {role.name.value}: {role.purpose} (tools: {', '.join(role.tool_categories)})"
        for role in ROLES.values()
        if role.name not in WORKSPACE_SCOPED_ROLES
    )
