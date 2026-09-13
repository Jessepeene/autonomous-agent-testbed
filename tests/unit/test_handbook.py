"""The handbook — and the drift guards that are the whole reason it is generated.

Hand-written documentation is worse than none, because an employee acts on it
confidently. So the tests that matter here are not "does it produce text" but
**can it produce text that is false**: a document naming a tool nobody has, a
department that does not exist, a roster missing the role that was added last
week, or a spend cap printed as a number when none is configured.

Every one of those is a real failure mode this codebase has already produced in
another form — `queue_review_patch` was registered as a tool the reviewer was
never told to call, twice.
"""

from __future__ import annotations

import re

import pytest

from factory import handbook
from factory.agents.roles import _HANDBOOK_RULES, ROLES
from factory.settings import EngineName, Settings
from factory.tools.handbook import build_handbook_tools
from factory.tools.registry import REGISTRY, ToolRegistry


def test_every_tool_the_rules_promise_actually_exists() -> None:
    """The core drift guard. `_HANDBOOK_RULES` is appended to every role's system
    prompt, so a tool named there and not registered is a promise made to all ten
    employees and kept for none — they would burn steps calling it and get an
    unknown-tool error, which is the confident-flailing pattern this layer exists
    to end."""
    registry = build_handbook_tools(role="analyst", task_id=None, product_id=None, engine=None)
    promised = set(re.findall(r"`(\w+)`", _HANDBOOK_RULES))
    missing = {name for name in promised if registry.get(name) is None}
    assert not missing, f"handbook rules promise tools that do not exist: {sorted(missing)}"


def test_the_directory_lists_every_employee() -> None:
    """Generated from `ROLES`, so adding a role cannot silently leave it out of the
    org chart the others read."""
    directory = handbook.org_directory()
    for name, role in ROLES.items():
        assert name.value in directory
        assert role.purpose[:40] in directory


def test_the_directory_says_which_employees_can_change_things() -> None:
    """A read-only role and a mutating one are genuinely different jobs, and
    "whose problem is this" usually turns on which."""
    directory = handbook.org_directory()
    for line in directory.splitlines():
        if line.startswith("- **"):
            assert ("can change things" in line) or ("read-only" in line)


def test_the_handoff_map_names_only_real_departments() -> None:
    """The one curated document here, so it is the one that can drift. It cannot
    drift into naming a department that does not exist."""
    named = set(re.findall(r"\*\*(\w+)\*\*", handbook.HANDOFF_MAP))
    real = {e.value.capitalize() for e in EngineName}
    assert named <= real, f"handoff map names unknown departments: {sorted(named - real)}"


def test_the_handoff_map_covers_every_department() -> None:
    """The inverse: a new engine must be given an owner in words, not left as a
    department nobody can look up."""
    named = {n.lower() for n in re.findall(r"\*\*(\w+)\*\*", handbook.HANDOFF_MAP)}
    assert {e.value for e in EngineName} <= named


def test_the_policy_brief_states_a_configured_cap_as_a_number() -> None:
    brief = handbook.policy_brief(Settings(daily_llm_spend_cap_usd=12.5))
    assert "$12.50" in brief


def test_the_policy_brief_does_not_invent_a_cap_that_is_not_set() -> None:
    """`daily_llm_spend_cap_usd` is genuinely optional. Formatting `None` as a
    number is the exact class of confidently-wrong documentation the module is
    built to avoid — and it crashed the first time it was run this way."""
    brief = handbook.policy_brief(Settings(daily_llm_spend_cap_usd=None))
    assert "No aggregate spend cap is configured" in brief
    assert "None" not in brief


@pytest.mark.parametrize("role", sorted(r.value for r in ROLES))
def test_every_employee_gets_the_handbook(role: str) -> None:
    registry = build_handbook_tools(role=role, task_id="t", product_id="p", engine=None)
    assert registry.get("document_work") is not None
    assert registry.get("read_product_dossier") is not None


def test_a_bound_write_tool_never_leaks_into_the_process_wide_registry() -> None:
    """`document_work` is bound to one employee on one task. If the per-run copy
    were shallow in the wrong direction, the *next* agent in this process would
    file its notes under the previous one's name."""
    build_handbook_tools(role="analyst", task_id="t1", product_id="p1", engine=None)
    assert REGISTRY.get("document_work") is None


def test_the_handbook_layers_onto_a_private_registry_without_losing_it() -> None:
    """WORKSPACE_SURGEON, SEO_OPTIMIZER and AUDITOR reach the runtime through
    builders that *replace* the registry rather than extend it. Composing has to
    keep both halves or those three read rules promising a handbook they lack."""
    base = ToolRegistry()

    @base.tool(
        name="private_thing",
        description="a per-run tool",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        category="workspace",
    )
    async def _private_thing() -> dict[str, str]:
        return {}

    registry = build_handbook_tools(
        role="workspace_surgeon", task_id="t", product_id="p", engine=None, base=base
    )
    assert registry.get("private_thing") is not None
    assert registry.get("document_work") is not None
    assert base.get("document_work") is None


def test_the_dossier_reports_a_missing_product_rather_than_inventing_one() -> None:
    """Fails the same direction as every other check here: say nothing is known,
    never fill the gap."""

    class _NoRows:
        async def execute(self, *_: object, **__: object) -> _NoRows:
            return self

        def first(self) -> None:
            return None

    import asyncio

    text = asyncio.run(handbook.product_dossier(_NoRows(), "nope"))  # type: ignore[arg-type]
    assert "No product" in text


def test_the_dossier_is_pinned_to_the_run_and_asks_for_no_id() -> None:
    """Found on the first live POC review. AUDITOR is never told its product id --
    its objective carries the URL and the product *name* -- so when it correctly
    decided it wanted the dossier, it guessed, passed the Vercel subdomain as the
    id, and got "No product with id ...". It then carried on without the context it
    had just tried to fetch, and the tool call was logged `ok: true`.

    The generic form asks the model for something only the caller knows. Same fix,
    and same reasoning, as `queue_review_patch` being pinned to its product."""
    registry = build_handbook_tools(
        role="auditor", task_id="t", product_id="prod-123", engine=None
    )
    descriptor = registry.get("read_product_dossier")
    assert descriptor is not None
    assert descriptor.input_schema["properties"] == {}


def test_without_a_product_the_dossier_still_takes_an_id() -> None:
    """A role spawned with no product -- a researcher answering an open question --
    keeps the general form rather than losing the tool entirely."""
    registry = build_handbook_tools(role="researcher", task_id="t", product_id=None, engine=None)
    descriptor = registry.get("read_product_dossier")
    assert descriptor is not None
    assert "product_id" in descriptor.input_schema["properties"]
