"""Surgical, in-place repair of a product workspace.

Repairing a product by regenerating its entire codebase is expensive (the whole
product goes into the prompt and comes back out, priced as code generation) and
blunt (the model never sees the real failure). `development/repair.py` runs a bounded
tool-using agent against the real workspace instead.

An agent editing a live workspace has a far larger blast radius than one returning
text, and unlike a regeneration there is no clean slate to fall back to -- so most of
what is tested here is containment: the snapshot/rollback, the evidence gate, and the
deterministic guards that catch a repair which made the suite green without fixing
anything.
"""

from __future__ import annotations

import pytest

from factory.agents.roles import RoleName, get_role
from factory.agents.runtime import AgentResult, _truncate_middle
from factory.engines.development import graph as dev_graph
from factory.engines.development import repair
from factory.settings import Settings
from factory.supervisor.context import FactoryContext


@pytest.fixture
def workspace(settings: Settings) -> str:
    root = settings.workspace_root / "product-1"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_main.py").write_text(
        "from app.main import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    return "product-1"


def _tree(settings: Settings, workspace: str) -> dict[str, str]:
    return repair._read_tree(settings.workspace_root / workspace)


# --- snapshot and rollback ------------------------------------------------------


def test_snapshot_restores_a_mangled_workspace_exactly(settings: Settings, workspace: str) -> None:
    before = _tree(settings, workspace)
    repair.take_snapshot(workspace, settings)

    root = settings.workspace_root / workspace
    (root / "app" / "main.py").write_text("garbage", encoding="utf-8")
    (root / "tests" / "test_main.py").unlink()
    (root / "junk.txt").write_text("scratch file", encoding="utf-8")

    assert repair.restore_snapshot(workspace, settings) is True
    assert _tree(settings, workspace) == before


def test_snapshot_preserves_binary_files_that_read_workspace_would_drop(
    settings: Settings, workspace: str
) -> None:
    """`read_workspace()` silently skips anything it cannot decode as UTF-8, so using
    it as the snapshot would permanently destroy a product's images or fonts on the
    first rollback. The snapshot is a real directory copy for exactly this reason."""
    logo = settings.workspace_root / workspace / "static" / "logo.png"
    logo.parent.mkdir(parents=True)
    logo.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff\xfe")

    repair.take_snapshot(workspace, settings)
    logo.unlink()
    repair.restore_snapshot(workspace, settings)

    assert logo.read_bytes() == b"\x89PNG\r\n\x1a\n\x00\xff\xfe"


def test_restore_keeps_the_knowledge_file_a_rollback_never_created(
    settings: Settings, workspace: str
) -> None:
    """`.factory` is deliberately outside the snapshot (it is notes, not product
    code). Deleting it during a restore would silently discard everything the product
    has learned about itself -- a one-way loss dressed up as a rollback."""
    repair.write_initial_knowledge(
        workspace, settings, product_name="Widget", summary="s" * 30, paths=["app/main.py"]
    )
    repair.take_snapshot(workspace, settings)
    (settings.workspace_root / workspace / "app" / "main.py").write_text("x", encoding="utf-8")

    repair.restore_snapshot(workspace, settings)

    assert "Widget" in repair.read_knowledge(workspace, settings)


def test_restore_reports_false_when_there_is_no_snapshot(
    settings: Settings, workspace: str
) -> None:
    assert repair.restore_snapshot(workspace, settings) is False


def test_clear_installed_deps_removes_the_stale_install_tree(
    settings: Settings, workspace: str
) -> None:
    """`clear_workspace` used to wipe `.deps` as a side effect of wiping everything.
    Nothing regenerates on the surgical path, and pip skips files already present in
    a `--target` directory -- so a stale tree makes a requirements bump a silent
    no-op."""
    deps = settings.workspace_root / workspace / ".deps" / "somepkg"
    deps.mkdir(parents=True)
    (deps / "__init__.py").write_text("old version", encoding="utf-8")

    repair.clear_installed_deps(workspace, settings)

    assert not (settings.workspace_root / workspace / ".deps").exists()


# --- Phase 8: the role's context budget reaches the runtime ---------------------


async def test_attempt_repair_passes_workspace_surgeons_max_context_chars(
    ctx: FactoryContext, workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-line wiring check, same shape as the pre-existing
    `cost_cap_usd=ctx.settings.repair_agent_cost_cap_usd` on the line above it in
    `repair.py` -- WORKSPACE_SURGEON never goes through `spawn()` (see
    WORKSPACE_SCOPED_ROLES), so this direct call site is the only place its
    `max_context_chars` can ever reach `AgentRuntime.run`."""
    captured: dict[str, object] = {}

    async def fake_run(self: object, **kwargs: object) -> AgentResult:
        captured.update(kwargs)
        return AgentResult(objective=str(kwargs.get("objective")), answer="", steps=0)

    monkeypatch.setattr(repair.AgentRuntime, "run", fake_run)

    await repair.attempt_repair(
        ctx,
        product_id="prod-1",
        workspace=workspace,
        test_command="pytest -q",
        healing="1 failed",
        recalled="",
    )

    role = get_role(RoleName.WORKSPACE_SURGEON)
    assert captured["max_context_chars"] == role.max_context_chars
    assert captured["cost_cap_usd"] == ctx.settings.repair_agent_cost_cap_usd


# --- objective framing: never tell the agent a false story about why it's here ---


async def _objective_for(
    ctx: FactoryContext,
    workspace: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    category: str,
    healing: str,
) -> str:
    captured: dict[str, object] = {}

    async def fake_run(self: object, **kwargs: object) -> AgentResult:
        captured.update(kwargs)
        return AgentResult(objective=str(kwargs.get("objective")), answer="", steps=0)

    monkeypatch.setattr(repair.AgentRuntime, "run", fake_run)
    await repair.attempt_repair(
        ctx,
        product_id="prod-1",
        workspace=workspace,
        test_command="pytest -q",
        healing=healing,
        recalled="",
        category=category,
    )
    return str(captured["objective"])


async def test_a_council_rejection_is_never_told_the_test_suite_is_failing(
    ctx: FactoryContext, workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """By the time Bug Council runs, the test suite has already passed -- telling
    WORKSPACE_SURGEON otherwise risks it hunting for a nonexistent failure instead of
    acting on Council's real finding."""
    objective = await _objective_for(
        ctx,
        workspace,
        monkeypatch,
        category="council_rejection:security",
        healing="STRIPE_API_KEY is never read from the environment.",
    )
    assert "test suite for this product is failing" not in objective
    assert "Bug Council" in objective
    assert "STRIPE_API_KEY is never read from the environment." in objective


async def test_a_guided_patch_is_never_told_the_test_suite_is_failing(
    ctx: FactoryContext, workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    objective = await _objective_for(
        ctx,
        workspace,
        monkeypatch,
        category="guided",
        healing="Read STRIPE_API_KEY, not STRIPE_SECRET_KEY.",
    )
    assert "test suite for this product is failing" not in objective
    # Framed as guidance to act on, not a failure to reproduce. Deliberately not
    # asserting the word "human": `guided` also carries AUDITOR's live-product
    # findings, and telling a repair agent a QA pass was "a human" is both false and
    # the wrong provenance -- the brief itself now states where a symptom was seen.
    assert "specific guidance" in objective.lower()
    assert "act on exactly what they said" in objective.lower()
    assert "Read STRIPE_API_KEY, not STRIPE_SECRET_KEY." in objective


async def test_a_real_test_failure_keeps_its_original_framing(
    ctx: FactoryContext, workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the new category-specific branches must not swallow the
    one case the generic framing is actually true for."""
    objective = await _objective_for(
        ctx, workspace, monkeypatch, category="test_failure", healing="1 failed"
    )
    assert "test suite for this product is failing" in objective


# --- the guards: green suite != fixed bug ---------------------------------------


def test_a_clean_repair_passes_the_audit(settings: Settings, workspace: str) -> None:
    before = _tree(settings, workspace)
    after = dict(before)
    after["app/main.py"] = "def add(a, b):\n    return a + b\n"

    assert repair.audit_edits(before, after) is None


def test_deleting_a_test_file_is_rejected(settings: Settings, workspace: str) -> None:
    before = _tree(settings, workspace)
    after = {name: body for name, body in before.items() if name != "tests/test_main.py"}

    violation = repair.audit_edits(before, after)

    assert violation is not None and "deleted test file" in violation


def test_removing_an_assertion_is_rejected(settings: Settings, workspace: str) -> None:
    before = _tree(settings, workspace)
    after = dict(before)
    after["tests/test_main.py"] = "from app.main import add\n\n\ndef test_add():\n    add(1, 2)\n"

    violation = repair.audit_edits(before, after)

    assert violation is not None and "assertion count" in violation


def test_adding_a_skip_marker_is_rejected(settings: Settings, workspace: str) -> None:
    before = _tree(settings, workspace)
    after = dict(before)
    after["tests/test_main.py"] = (
        "import pytest\nfrom app.main import add\n\n\n"
        "@pytest.mark.skip\ndef test_add():\n    assert add(1, 2) == 3\n"
    )

    violation = repair.audit_edits(before, after)

    assert violation is not None and "skip/xfail" in violation


def test_adding_an_xfail_marker_is_rejected(settings: Settings, workspace: str) -> None:
    before = _tree(settings, workspace)
    after = dict(before)
    after["tests/test_main.py"] = (
        "import pytest\nfrom app.main import add\n\n\n"
        "@pytest.mark.xfail\ndef test_add():\n    assert add(1, 2) == 3\n"
    )

    assert repair.audit_edits(before, after) is not None


def test_deleting_requirements_is_rejected(settings: Settings, workspace: str) -> None:
    before = _tree(settings, workspace)
    after = {name: body for name, body in before.items() if name != "requirements.txt"}

    violation = repair.audit_edits(before, after)

    assert violation is not None and "requirements.txt" in violation


def test_adding_a_test_is_not_penalised(settings: Settings, workspace: str) -> None:
    """A repair that adds coverage for the bug it fixed is the good case -- the
    assertion-count guard must not read it as tampering."""
    before = _tree(settings, workspace)
    after = dict(before)
    after["tests/test_regression.py"] = "def test_new():\n    assert True\n"

    assert repair.audit_edits(before, after) is None


# --- the knowledge file ----------------------------------------------------------


def test_knowledge_is_seeded_from_the_plan_without_any_model_call(
    settings: Settings, workspace: str
) -> None:
    repair.write_initial_knowledge(
        workspace,
        settings,
        product_name="Invoice Reconciler",
        summary="Matches invoices against bank lines automatically.",
        paths=["app/main.py", "tests/test_main.py"],
    )

    text = repair.read_knowledge(workspace, settings)

    assert "Invoice Reconciler" in text
    assert "Matches invoices against bank lines" in text
    assert "app/main.py" in text


def test_knowledge_file_is_never_reviewed_or_shipped(settings: Settings, workspace: str) -> None:
    """It is notes *about* the product, not part of it. Reviewing it would spend
    Council tokens on prose no user runs; shipping it would publish the factory's
    internal commentary next to the product."""
    from factory.engines.development.deploy import _collect_files
    from factory.engines.development.scaffold.generator import read_workspace

    repair.write_initial_knowledge(
        workspace, settings, product_name="W", summary="s" * 30, paths=["app/main.py"]
    )

    reviewed = read_workspace(workspace, settings)
    shipped = {entry["file"] for entry in _collect_files(workspace, settings)}

    assert not any(".factory" in name for name in reviewed)
    assert not any(".factory" in name for name in shipped)


def test_a_verified_repair_is_appended_to_the_history(settings: Settings, workspace: str) -> None:
    repair.write_initial_knowledge(
        workspace, settings, product_name="W", summary="s" * 30, paths=["app/main.py"]
    )

    repair.record_verified_repair(
        workspace,
        settings,
        changed_files={"app/main.py"},
        note="add() subtracted instead of adding. RESULT: FIXED",
    )

    text = repair.read_knowledge(workspace, settings)
    assert "app/main.py" in text
    assert "subtracted instead of adding" in text
    assert "_None yet._" not in text


def test_repair_history_is_bounded(settings: Settings, workspace: str) -> None:
    """This file is injected into every repair prompt, so unbounded growth would
    quietly reintroduce the cost it exists to avoid."""
    repair.write_initial_knowledge(
        workspace, settings, product_name="W", summary="s" * 30, paths=["app/main.py"]
    )
    for i in range(repair.MAX_HISTORY_ENTRIES + 8):
        repair.record_verified_repair(
            workspace, settings, changed_files={"app/main.py"}, note=f"fix number {i}"
        )

    text = repair.read_knowledge(workspace, settings)
    entries = [line for line in text.splitlines() if line.startswith("- Fixed")]

    assert len(entries) == repair.MAX_HISTORY_ENTRIES
    # The oldest are dropped, not the newest.
    assert f"fix number {repair.MAX_HISTORY_ENTRIES + 7}" in text
    assert "fix number 0" not in text


def test_recording_against_a_missing_knowledge_file_is_a_no_op(
    settings: Settings, workspace: str
) -> None:
    repair.record_verified_repair(workspace, settings, changed_files={"a.py"}, note="whatever")
    assert repair.read_knowledge(workspace, settings) == ""


# --- routing: which failures a surgical repair may attempt -----------------------


class _FakeProduct:
    def __init__(self, *, test_command: str | None = "pytest -q", heal_attempts: int = 0) -> None:
        self.id = "prod-1"
        self.name = "Widget"
        self.idea_id = "idea-1"
        self.test_command = test_command
        self.heal_attempts = heal_attempts


async def _routes(ctx: FactoryContext, product: _FakeProduct, category: str) -> bool:
    ctx.docker_available = True
    return await dev_graph._should_repair_surgically(
        ctx,
        None,  # type: ignore[arg-type]
        product,  # type: ignore[arg-type]
        category,
    )


async def test_a_test_failure_routes_to_a_surgical_repair(ctx: FactoryContext) -> None:
    assert await _routes(ctx, _FakeProduct(), "test_failure") is True


@pytest.mark.parametrize("category", ["dependency_failure", "dependency_timeout", "deploy_failure"])
async def test_failures_a_surgical_repair_cannot_fix_regenerate_instead(
    ctx: FactoryContext, category: str
) -> None:
    """A sandbox with no network cannot run pip; a deploy failure is not a workspace
    bug -- neither is something an in-place edit could ever address."""
    assert await _routes(ctx, _FakeProduct(), category) is False


@pytest.mark.parametrize(
    "category", ["council_rejection:security", "council_rejection:pricing,security"]
)
async def test_a_council_rejection_routes_to_a_surgical_repair(
    ctx: FactoryContext, category: str
) -> None:
    """Real bug, found live: 4 separate products burned a full blind regeneration on
    every one of 3 attempts for the identical, narrowly-mechanical Council finding
    (wrong Stripe key env var name), because council_rejection was never eligible for
    a targeted repair -- despite Council's own healing text already saying exactly
    what to change. Every category shape is checked: a single blocked category and
    multiple comma-joined ones (see graph.py's `f"council_rejection:{','.join(...)}"`)."""
    assert await _routes(ctx, _FakeProduct(), category) is True


async def test_a_council_rejection_still_regenerates_on_the_last_autonomous_cycle(
    ctx: FactoryContext,
) -> None:
    """Unlike `guided`, a council rejection is an autonomous retry -- the same
    two-surgical-attempts-then-rebuild ceiling applies to it as to test_failure."""
    last = _FakeProduct(heal_attempts=dev_graph.MAX_HEAL_CYCLES - 1)
    assert await _routes(ctx, last, "council_rejection:security") is False


async def test_a_product_with_no_recorded_test_command_regenerates(
    ctx: FactoryContext,
) -> None:
    """Guessing a default would have the agent optimising against a different suite
    than the verification gate runs -- success right up until it is checked."""
    assert await _routes(ctx, _FakeProduct(test_command=None), "test_failure") is False


async def test_the_last_autonomous_cycle_always_regenerates(ctx: FactoryContext) -> None:
    """Two surgical attempts have already failed; a from-scratch rebuild is the only
    thing left that can recover a workspace they may have made worse."""
    last = _FakeProduct(heal_attempts=dev_graph.MAX_HEAL_CYCLES - 1)
    assert await _routes(ctx, last, "test_failure") is False


async def test_a_human_guided_patch_stays_surgical_past_the_cycle_ceiling(
    ctx: FactoryContext,
) -> None:
    """A guided patch only ever exists because a human replied to an escalation, so it
    arrives with heal_attempts already maxed. Regenerating a live product from scratch
    over one human instruction would discard everything that works to fix one thing --
    and this is the best-informed repair the factory ever performs."""
    exhausted = _FakeProduct(heal_attempts=dev_graph.MAX_HEAL_CYCLES)
    assert await _routes(ctx, exhausted, "guided") is True


async def test_no_sandbox_means_no_surgical_repair(ctx: FactoryContext) -> None:
    ctx.docker_available = False
    assert (
        await dev_graph._should_repair_surgically(
            ctx,
            None,  # type: ignore[arg-type]
            _FakeProduct(),  # type: ignore[arg-type]
            "test_failure",
        )
        is False
    )


def test_guided_patches_carry_the_category_that_routes_them() -> None:
    """Regression guard for a real gap: `enqueue_guided_patch` built its payload
    without a category, so a human's diagnosed fix fell through to the most expensive
    path available -- full regeneration of a live product."""
    import inspect

    from factory.state import repository

    source = inspect.getsource(repository.enqueue_guided_patch)
    assert '"category": "guided"' in source
    assert "guided" in dev_graph.SURGICAL_CATEGORIES


def test_dependency_not_on_pypi_is_surgical_but_the_other_two_are_not() -> None:
    """`dependency_not_on_pypi` is the one `dependency_*` category that IS
    surgically repairable -- the diagnosis re-runs in-process (real PyPI, no
    sandbox network needed), unlike `dependency_timeout`/`dependency_failure`,
    which need the repair agent's own sandbox to actually retry an install it
    has no network to attempt. Getting this wrong either way is expensive: too
    narrow forces full regeneration for a one-line requirements.txt fix; too
    wide sends a real infra failure to an agent that cannot possibly fix it."""
    assert "dependency_not_on_pypi" in dev_graph.SURGICAL_CATEGORIES
    assert "dependency_timeout" not in dev_graph.SURGICAL_CATEGORIES
    assert "dependency_failure" not in dev_graph.SURGICAL_CATEGORIES


# --- the workspace tools ---------------------------------------------------------


class _RecordingSandbox:
    available = True

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def execute(
        self,
        command: str,
        *,
        workspace: str,
        timeout_seconds: int | None = None,
        network: bool = False,
        env: dict[str, str] | None = None,
    ) -> object:
        self.calls.append(
            {"command": command, "workspace": workspace, "network": network, "env": env}
        )
        from factory.sandbox.runner import SandboxResult

        return SandboxResult(exit_code=0, stdout="2 passed", stderr="")


def _tools(settings: Settings, workspace: str, sandbox: object | None = None) -> dict[str, object]:
    from factory.tools.workspace import build_workspace_tools

    registry = build_workspace_tools(
        workspace=workspace,
        settings=settings,
        sandbox=sandbox or _RecordingSandbox(),  # type: ignore[arg-type]
        test_command="pytest -q",
    )
    return {d.name: d.handler for d in registry.all()}


async def test_the_agent_cannot_choose_which_workspace_it_edits(
    settings: Settings, workspace: str
) -> None:
    """`run_in_sandbox` takes `workspace` as a model argument, and `resolve_workspace`
    *creates* the directory it resolves -- so a hallucinated name silently produces an
    empty directory where every command "succeeds" against nothing. These tools close
    over the workspace instead, so no schema exposes it."""
    from factory.tools.workspace import build_workspace_tools

    registry = build_workspace_tools(
        workspace=workspace,
        settings=settings,
        sandbox=_RecordingSandbox(),  # type: ignore[arg-type]
        test_command="pytest -q",
    )
    for descriptor in registry.all():
        assert "workspace" not in descriptor.input_schema.get("properties", {})


async def test_reading_a_path_outside_the_workspace_is_refused(
    settings: Settings, workspace: str
) -> None:
    handlers = _tools(settings, workspace)
    result = await handlers["read_workspace_file"](path="../../etc/passwd")  # type: ignore[operator]
    assert "outside the workspace" in result["error"]


async def test_writing_a_path_outside_the_workspace_is_refused(
    settings: Settings, workspace: str
) -> None:
    handlers = _tools(settings, workspace)
    result = await handlers["write_workspace_file"](  # type: ignore[operator]
        path="../escape.py", content="x"
    )
    assert "outside the workspace" in result["error"]
    assert not (settings.workspace_root / "escape.py").exists()


async def test_the_agent_cannot_edit_its_own_knowledge_file_or_installed_deps(
    settings: Settings, workspace: str
) -> None:
    """Editing a vendored library to make a test pass is never the fix, and the
    knowledge file is the factory's record about the product, not the agent's to
    rewrite mid-repair."""
    handlers = _tools(settings, workspace)

    deps = await handlers["write_workspace_file"](  # type: ignore[operator]
        path=".deps/somepkg/__init__.py", content="raise SystemExit"
    )
    notes = await handlers["write_workspace_file"](  # type: ignore[operator]
        path=".factory/ARCHITECTURE.md", content="all good, nothing to see"
    )

    assert "internal directory" in deps["error"]
    assert "internal directory" in notes["error"]


async def test_a_write_then_read_round_trips(settings: Settings, workspace: str) -> None:
    handlers = _tools(settings, workspace)
    await handlers["write_workspace_file"](  # type: ignore[operator]
        path="app/main.py", content="def add(a, b):\n    return a + b\n"
    )
    result = await handlers["read_workspace_file"](path="app/main.py")  # type: ignore[operator]
    assert result["content"] == "def add(a, b):\n    return a + b\n"


async def test_listing_hides_dependencies_and_internal_notes(
    settings: Settings, workspace: str
) -> None:
    """A raw `ls -R` over a workspace with installed dependencies returns thousands of
    vendored files, which would be truncated into pure noise and burn the agent's step
    budget before it ever saw the product's own code."""
    deps = settings.workspace_root / workspace / ".deps" / "pytest"
    deps.mkdir(parents=True)
    (deps / "__init__.py").write_text("vendored", encoding="utf-8")
    repair.write_initial_knowledge(
        workspace, settings, product_name="W", summary="s" * 30, paths=["app/main.py"]
    )

    handlers = _tools(settings, workspace)
    listed = await handlers["list_workspace_files"]()  # type: ignore[operator]
    paths = {entry["path"] for entry in listed["files"]}

    assert "app/main.py" in paths
    assert not any(p.startswith(".deps") for p in paths)
    assert not any(p.startswith(".factory") for p in paths)


async def test_the_agent_runs_the_suite_exactly_as_the_gate_will(
    settings: Settings, workspace: str
) -> None:
    """If the agent's reproduce differed from the verification gate's invocation, it
    would be optimising against a command nobody checks -- which looks like success
    right up until the gate runs and fails."""
    sandbox = _RecordingSandbox()
    handlers = _tools(settings, workspace, sandbox)

    await handlers["run_workspace_command"](command="pytest -q")  # type: ignore[operator]

    sent = sandbox.calls[0]
    assert sent["env"] == {"PYTHONPATH": "/workspace/.deps"}
    assert "/workspace/.deps/bin:$PATH" in str(sent["command"])
    assert sent["network"] is False, "an agent-run command must never reach the network"
    assert sent["workspace"] == workspace


# --- tool-result truncation ------------------------------------------------------


def test_truncation_keeps_the_tail_where_the_failure_summary_lives() -> None:
    """Head-only truncation cut `stderr` off the end of a tool result entirely: the
    JSON keys are ordered exit_code, stdout, stderr, so a chatty pytest run filled
    the budget with collection output and passing dots. `_verify_and_ship` has always
    taken the *tail* of test output for exactly this reason."""
    content = "COLLECTION NOISE " * 2000 + "E   AssertionError: assert 400 == 201"

    truncated = _truncate_middle(content, 500)

    assert len(truncated) <= 500
    assert "AssertionError: assert 400 == 201" in truncated
    assert truncated.startswith("COLLECTION NOISE")


def test_truncation_marks_the_gap_it_created() -> None:
    truncated = _truncate_middle("x" * 5_000, 400)
    assert "elided" in truncated


def test_short_results_are_returned_untouched() -> None:
    assert _truncate_middle("all 5 tests passed", 500) == "all 5 tests passed"
