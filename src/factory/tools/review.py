"""The Bug Council reviewers, as tools an engineer can use on their own work.

Bug Council was only ever a gate: six judgements that ran once, at
`_verify_and_ship`, after the engineer had finished and gone. It is not an employee
-- no `AgentRole`, no capability envelope, nothing in the org directory -- and it
sees the whole source corpus rather than the change, so it cannot tell "you broke
this" from "this was always here".

The practical cost of gate-only review is thrashing. The loop was: repair, finish,
gate, findings, new heal cycle, repair, finish, gate, *different* findings. Observed
twice in one afternoon on one product, each cycle trading one set of blockers for
another and never converging, because nothing let an engineer see a finding while
it was still cheap to fix.

A human team has both halves: CI runs the checks on every push, *and* a developer
runs the linter locally before pushing. This module is the second half. The gate in
`_verify_and_ship` is unchanged and still mandatory -- "nothing ships unreviewed"
is a rail, and a review an agent can decline to run is not a rail.

What changes is that an engineer can now ask, mid-run, "is what I just wrote going
to pass?" and fix it in the same run instead of a cycle later.

Pinned to one workspace per run, like every other workspace tool: the agent names a
category, never a path, so it cannot point a reviewer at code it was not sent to
work on.
"""

from __future__ import annotations

from typing import Any

from factory.engines.development.bug_council import (
    REVIEWER_PROMPTS,
    _model_review,
    render_sources,
    review_corpus,
    scan_privilege,
    scan_secrets,
)
from factory.engines.development.scaffold.generator import read_workspace
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.state.schemas import ReviewCategory, ReviewVerdict
from factory.supervisor.context import FactoryContext
from factory.tools.registry import ToolRegistry

log = get_logger(__name__)

#: Run with no model call at all, so they are always included and never cost
#: anything. An engineer should not have to choose to check for a committed
#: credential.
_FREE_SCANNERS = ("secrets", "privilege")


def _render(verdict: ReviewVerdict) -> list[dict[str, Any]]:
    return [
        {
            "category": str(finding.category),
            "severity": finding.severity,
            "file": finding.file,
            "line": finding.line,
            "problem": finding.summary,
            "why_it_matters": finding.failure_scenario,
            "fix": finding.remediation,
        }
        for finding in verdict.findings
    ]


def build_review_tools(ctx: FactoryContext, *, workspace: str) -> ToolRegistry:
    """A private registry letting one agent review the workspace it is working in."""
    registry = ToolRegistry()

    @registry.tool(
        name="list_reviewers",
        description=(
            "List the code reviewers you can run against your own work, and what "
            "each one looks for. These are the same reviewers that decide whether "
            "your change is allowed to ship, so anything they would block on is "
            "worth finding now rather than after you finish."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        category="review",
    )
    async def list_reviewers() -> dict[str, Any]:
        return {
            "reviewers": [
                {"category": str(category), "looks_for": prompt.strip()[:300]}
                for category, prompt in REVIEWER_PROMPTS.items()
            ],
            "always_run_free": list(_FREE_SCANNERS),
            "note": (
                "secrets and privilege are pattern scans and cost nothing, so they "
                "run on every call. The others each cost a model call -- ask for the "
                "ones relevant to what you changed rather than all of them."
            ),
        }

    @registry.tool(
        name="review_my_work",
        description=(
            "Run code reviewers over the current state of your workspace and get "
            "their findings back, before you finish. Use this after making a change "
            "that touches auth, payments, input handling, dependencies or anything "
            "performance-sensitive. Fixing a finding now costs you a step; finding "
            "out after you finish costs a whole repair cycle. Secrets and privilege "
            "scans always run and are free."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {"type": "string", "enum": [str(c) for c in REVIEWER_PROMPTS]},
                    "description": (
                        "Which model reviewers to run, e.g. ['security'] after "
                        "touching auth. Omit for the free scans only."
                    ),
                }
            },
            "additionalProperties": False,
        },
        category="review",
    )
    async def review_my_work(categories: list[str] | None = None) -> dict[str, Any]:
        sources = read_workspace(workspace, ctx.settings)
        if not sources:
            return {"error": "the workspace has no source files to review"}

        findings: list[dict[str, Any]] = []
        # Free first, always. A committed credential is not something an engineer
        # should have had to think to ask about.
        for verdict in (scan_secrets(sources), scan_privilege(sources)):
            findings += _render(verdict)

        requested: list[ReviewCategory] = []
        for name in categories or []:
            try:
                category = ReviewCategory(name)
            except ValueError:
                continue
            if category in REVIEWER_PROMPTS:
                requested.append(category)

        if requested:
            rendered, truncated = render_sources(sources)
            corpus = review_corpus(rendered, truncated=truncated)
            for category in requested:
                try:
                    verdict = await _model_review(ctx, category, corpus, task_id=None)
                except LLMError as exc:
                    # A reviewer that could not run has rendered no opinion. Saying
                    # so is the honest answer; reporting "no findings" would read as
                    # a clean bill of health it never gave.
                    findings.append(
                        {
                            "category": str(category),
                            "severity": "unknown",
                            "problem": f"this reviewer could not run: {str(exc)[:200]}",
                            "fix": "treat this category as unreviewed, not as clean",
                        }
                    )
                    continue
                findings += _render(verdict)

        blocking = [f for f in findings if f.get("severity") in ("critical", "high")]
        log.info(
            "review.self_review",
            workspace=workspace,
            requested=[str(c) for c in requested],
            findings=len(findings),
        )
        return {
            "findings": findings,
            "total": len(findings),
            "would_block_deploy": len(blocking),
            "verdict": (
                "Nothing found. This is not a guarantee -- only the reviewers you ran had a say."
                if not findings
                else (
                    f"{len(findings)} finding(s); {len(blocking)} at a severity "
                    "that stops a deploy."
                )
            ),
        }

    return registry


__all__ = ["build_review_tools"]
