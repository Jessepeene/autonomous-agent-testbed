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
    """Rendered for the Orchestrator prompt so it can pick a role by name."""
    return "\n".join(
        f"- {role.name.value}: {role.purpose} (tools: {', '.join(role.tool_categories)})"
        for role in ROLES.values()
    )
