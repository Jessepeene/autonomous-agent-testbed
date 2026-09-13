"""Bug Council's reviewers, usable by the engineer rather than only by the gate.

The gate was the only place review ever happened: six judgements, once, after the
engineer had finished and gone. The cost of that was thrashing -- repair, finish,
gate, findings, new heal cycle, repair, finish, gate, *different* findings. Two
runs on one product in an afternoon, each trading one set of blockers for another
and never converging.

These tests pin the two properties that make the tool version safe: it cannot be
pointed at code the agent was not sent to work on, and it never reports a reviewer
that failed to run as a clean result.
"""

from __future__ import annotations

import pytest

from factory.agents.roles import ROLES, RoleName
from factory.tools.review import build_review_tools


@pytest.fixture
def registry(ctx, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    from factory.engines.development.scaffold import generator

    monkeypatch.setattr(
        generator, "read_workspace", lambda workspace, settings=None: {"app/main.py": "x = 1"}
    )
    return build_review_tools(ctx, workspace="product-1")


def test_the_reviewers_are_offered_as_tools(registry) -> None:  # type: ignore[no-untyped-def]
    assert registry.get("list_reviewers") is not None
    assert registry.get("review_my_work") is not None


def test_the_agent_cannot_choose_what_to_review(registry) -> None:  # type: ignore[no-untyped-def]
    """Pinned to one workspace, like every other workspace tool. The agent names a
    category, never a path, so it cannot point a reviewer at code it was not sent to
    work on."""
    schema = registry.get("review_my_work").input_schema
    assert set(schema["properties"]) == {"categories"}
    assert "workspace" not in schema["properties"]
    assert "path" not in schema["properties"]


def test_only_real_reviewer_categories_are_offered(registry) -> None:  # type: ignore[no-untyped-def]
    from factory.engines.development.bug_council import REVIEWER_PROMPTS

    offered = registry.get("review_my_work").input_schema["properties"]["categories"]["items"]
    assert set(offered["enum"]) == {str(c) for c in REVIEWER_PROMPTS}


def test_the_repair_role_can_actually_reach_them() -> None:
    """A tool nobody is granted does not get called -- the same failure as
    `queue_review_patch`, which was registered twice and invoked never."""
    assert "review" in ROLES[RoleName.WORKSPACE_SURGEON].tool_categories


def test_the_repair_role_is_told_to_use_them() -> None:
    """And a tool nobody is *told about* does not get called either."""
    prompt = ROLES[RoleName.WORKSPACE_SURGEON].system_prompt
    assert "review_my_work" in prompt


async def test_a_reviewer_that_could_not_run_is_not_reported_as_clean(
    ctx, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """The honest-failure rule. A reviewer that errored rendered no opinion, and
    returning "no findings" for it would read as a clean bill of health it never
    gave -- the same mistake as counting a provider outage as a passing review."""
    from factory.engines.development import bug_council
    from factory.llm.providers.base import LLMError
    from factory.tools import review as review_module

    monkeypatch.setattr(
        review_module, "read_workspace", lambda workspace, settings=None: {"app/main.py": "x = 1"}
    )

    async def boom(*args: object, **kwargs: object) -> None:
        raise LLMError("provider is down")

    monkeypatch.setattr(review_module, "_model_review", boom)
    monkeypatch.setattr(review_module, "scan_secrets", lambda s: bug_council.scan_secrets({}))
    monkeypatch.setattr(review_module, "scan_privilege", lambda s: bug_council.scan_privilege({}))

    registry = build_review_tools(ctx, workspace="product-1")
    result = await registry.get("review_my_work").handler(categories=["security"])

    problems = [f["problem"] for f in result["findings"]]
    assert any("could not run" in p for p in problems)
    assert "unreviewed" in str(result["findings"])
