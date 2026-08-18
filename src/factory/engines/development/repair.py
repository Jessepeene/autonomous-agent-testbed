"""Surgical, in-place repair of a product workspace.

The Development Engine's original repair strategy was to regenerate the entire
codebase from a text description of what broke. That is expensive (the whole product
goes into the prompt *and* comes back out, priced as code generation) and blunt: the
model never sees the actual failure, only `graph.py`'s truncated summary of it.

This module runs a bounded tool-using agent against the real workspace instead. It
reads the real files, runs the real test suite, edits what is broken, and re-runs.

Everything here exists because an agent editing a live workspace has a much larger
blast radius than one returning text, and unlike a regeneration there is no clean
slate to fall back to:

* **Snapshot before, restore on any bad outcome.** A product workspace is not a git
  repository -- this snapshot is the only rollback that exists.
* **Evidence, not self-report.** An agent that says it fixed the bug but never called
  a tool has not fixed the bug.
* **Deterministic guards over prompt instructions.** The system prompt forbids
  deleting tests; these checks are what make it true. Same reasoning as
  `bug_council`'s `scan_secrets`/`scan_privilege`: a question with a correct answer
  must not depend on a model's mood.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from factory.agents.roles import RoleName, get_role
from factory.agents.runtime import AgentRuntime
from factory.engines.development.scaffold.generator import WORKSPACE_SKIP_DIRS
from factory.llm.router import model_for
from factory.logging import get_logger
from factory.sandbox.runner import resolve_workspace
from factory.settings import EngineName, Settings
from factory.supervisor.context import FactoryContext
from factory.tools.workspace import build_workspace_tools

log = get_logger(__name__)

#: Tool results are the bulk of an agent transcript and the transcript is re-sent
#: every step, so this is the single biggest lever on cost. Well under the 20k
#: default: a truncated pytest run keeps its head and tail (`_truncate_middle`),
#: which is where the failure actually is.
MAX_RESULT_CHARS = 6_000

#: Outcomes that mean "the agent never got a fair attempt" rather than "the repair
#: failed". These must not consume a heal cycle -- see `RepairBlocked`.
BLOCKED_REASONS = frozenset(
    {
        "no_provider",
        "provider_error",
        "deadline",
        "step_limit",
        "cost_cap",
        "empty_response",
        "truncated",
        "not_permitted",
    }
)

_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
_ASSERT = re.compile(r"\bassert\b")
_WEAKENER = re.compile(r"@pytest\.mark\.(skip|xfail)\b|pytest\.(skip|xfail)\(")


class RepairBlocked(RuntimeError):
    """The repair could not be attempted for a reason the product did not cause.

    A provider outage, an exhausted step/cost budget, or a truncated response says
    nothing about whether the code is fixable -- so the caller defers the task rather
    than burning one of its three heal cycles. Mirrors `CampaignApprovalPending`,
    which `supervisor/graph.py` already defers for the same reason.
    """


@dataclass(slots=True)
class RepairOutcome:
    changed_files: set[str]
    answer: str
    summary: str
    rejected_because: str | None = None

    @property
    def usable(self) -> bool:
        return bool(self.changed_files) and self.rejected_because is None


def _snapshot_path(workspace: str, settings: Settings) -> Path:
    """A sibling of the workspace, never a child of it.

    A child would be walked by `read_workspace` (so the Bug Council would review a
    duplicate copy of every file) and by `deploy._collect_files` (so it would ship).
    Both of those walk exactly one workspace directory, so a sibling is invisible to
    both.
    """
    return settings.workspace_root.resolve() / f"{workspace}.snapshot"


def _copy_tree(src: Path, dst: Path) -> None:
    """Merge `src` into `dst`, skipping the same directories every other reader skips.

    Deliberately does *not* clear `dst` first. It used to, which silently defeated
    the selective cleanup in `restore_snapshot`: that function carefully preserves
    `WORKSPACE_SKIP_DIRS` entries (notably `.factory`, the accumulated knowledge
    file, which is not in the snapshot because it is not product code) and then
    called this -- which promptly deleted the whole destination anyway, turning every
    rollback into a permanent loss of everything the product had learned. Callers
    that genuinely want a clean destination clear it themselves.
    """
    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns(*WORKSPACE_SKIP_DIRS),
        dirs_exist_ok=True,
    )


def take_snapshot(workspace: str, settings: Settings) -> Path:
    """Byte-exact copy of the workspace, for rollback.

    Deliberately a real directory copy rather than `read_workspace()`'s dict: that
    helper silently drops every file it cannot decode as UTF-8 and everything under
    `WORKSPACE_SKIP_DIRS`, so restoring from it would permanently destroy a product's
    images, fonts, or any other binary asset on the very first rollback.
    """
    root = resolve_workspace(workspace, settings)
    snapshot = _snapshot_path(workspace, settings)
    # A snapshot must be a replacement, never a merge: a leftover from an earlier
    # attempt could otherwise contribute files this workspace no longer has.
    shutil.rmtree(snapshot, ignore_errors=True)
    _copy_tree(root, snapshot)
    return snapshot


def restore_snapshot(workspace: str, settings: Settings) -> bool:
    """Put the workspace back as it was. Returns False when there is nothing to do."""
    snapshot = _snapshot_path(workspace, settings)
    if not snapshot.is_dir():
        return False
    root = resolve_workspace(workspace, settings)
    for entry in root.iterdir():
        # Skip-dirs are deliberately absent from the snapshot, so deleting them here
        # would be a one-way loss rather than a restore: `.factory` holds this
        # product's accumulated knowledge file, which every future repair reads and
        # which no rollback should ever discard. (`.deps` is merely regenerated.)
        if entry.name in WORKSPACE_SKIP_DIRS:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    _copy_tree(snapshot, root)
    shutil.rmtree(snapshot, ignore_errors=True)
    log.info("repair.snapshot_restored", workspace=workspace)
    return True


def discard_snapshot(workspace: str, settings: Settings) -> None:
    shutil.rmtree(_snapshot_path(workspace, settings), ignore_errors=True)


# --- the per-product knowledge file ---------------------------------------------
#
# What makes a repair surgical is knowing how the product is put together *before*
# opening any files. Without that, every repair on a month-old product re-derives the
# same structure from scratch -- which is both the slow way and the expensive way,
# since re-reading twenty files costs an order of magnitude more tokens than reading
# one page of notes.
#
# Deliberately a plain file in the workspace rather than a row in the database or a
# long-lived process holding context: it survives reboots, deploys and crashes for
# free, a human can read and correct it, and it travels with the product it describes.
#
# Written at build time from the `ScaffoldPlan` that is *already* in memory, so it
# costs no extra LLM call, and appended to only after a repair has actually been
# verified -- a stale or speculative note is worse than none, because it misleads
# confidently.

_KNOWLEDGE_PATH = ".factory/ARCHITECTURE.md"

#: Kept short on purpose: this is injected into every repair prompt, so unbounded
#: growth would quietly reintroduce the very cost this exists to avoid.
MAX_KNOWLEDGE_CHARS = 6_000
MAX_HISTORY_ENTRIES = 12


def _knowledge_file(workspace: str, settings: Settings) -> Path:
    return resolve_workspace(workspace, settings) / _KNOWLEDGE_PATH


def write_initial_knowledge(
    workspace: str, settings: Settings, *, product_name: str, summary: str, paths: list[str]
) -> None:
    """Seed the knowledge file from the plan that just built the product.

    Every field here was already generated and paid for; nothing new is asked of a
    model. Overwrites on each full (re)generation, because after a regeneration the
    previous structure and the repair history against it no longer describe the code
    that is now on disk.
    """
    path = _knowledge_file(workspace, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    listed = "\n".join(f"- {p}" for p in sorted(paths)[:80])
    path.write_text(
        f"# {product_name}\n\n"
        "Notes for whoever repairs this product next. Not shipped, not reviewed.\n\n"
        f"## What it does\n\n{summary}\n\n"
        f"## Files\n\n{listed}\n\n"
        "## Repair history\n\n"
        "_None yet._\n",
        encoding="utf-8",
    )
    log.info("repair.knowledge_seeded", workspace=workspace, files=len(paths))


def read_knowledge(workspace: str, settings: Settings) -> str:
    path = _knowledge_file(workspace, settings)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:MAX_KNOWLEDGE_CHARS]
    except OSError:
        return ""


def record_verified_repair(
    workspace: str, settings: Settings, *, changed_files: set[str], note: str
) -> None:
    """Append one entry, but only once the suite and the Bug Council have both passed.

    Recording at the point the agent merely *claimed* success would fill this file
    with confident accounts of fixes that never worked, and every later repair would
    read them as established fact.
    """
    path = _knowledge_file(workspace, settings)
    if not path.is_file():
        return
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        return

    head, _, history = existing.partition("## Repair history\n")
    entries = [line for line in history.splitlines() if line.startswith("- ")]
    entries.append(f"- Fixed ({', '.join(sorted(changed_files)[:5])}): {note.strip()[:300]}")
    kept = entries[-MAX_HISTORY_ENTRIES:]
    path.write_text(
        f"{head}## Repair history\n\n" + "\n".join(kept) + "\n",
        encoding="utf-8",
    )


def clear_installed_deps(workspace: str, settings: Settings) -> None:
    """Remove the installed-dependency tree so the next install is fresh.

    `clear_workspace()` used to do this implicitly on every heal cycle, because it
    wiped everything. The agentic path deliberately does not wipe the workspace --
    but `_verify_and_ship` installs with `--target` and no `--upgrade`, and pip
    *skips* files already present in a `--target` directory. Left in place, a
    genuine `requirements.txt` version bump would silently install nothing, the
    suite would fail identically, and neither the agent nor the log would show why.
    """
    shutil.rmtree(resolve_workspace(workspace, settings) / ".deps", ignore_errors=True)


def _read_tree(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in WORKSPACE_SKIP_DIRS for part in rel.parts):
            continue
        try:
            files[str(rel).replace("\\", "/")] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary assets are compared by existence only -- enough to notice a
            # deletion, and their content is not something a repair should touch.
            files[str(rel).replace("\\", "/")] = "<binary>"
    return files


def _assert_count(files: dict[str, str]) -> int:
    return sum(
        len(_ASSERT.findall(body)) for name, body in files.items() if _TEST_PATH.search(name)
    )


def audit_edits(before: dict[str, str], after: dict[str, str]) -> str | None:
    """Why this repair must be rolled back, or ``None`` if it is acceptable.

    Every rule here targets a way of making the suite green without fixing anything,
    which is the failure mode that matters: a repair that deletes the failing test
    passes verification, passes Bug Council (there is nothing left to object to), and
    ships a product whose bug is now invisible.
    """
    removed_tests = sorted(name for name in before if _TEST_PATH.search(name) and name not in after)
    if removed_tests:
        return f"deleted test file(s): {', '.join(removed_tests[:5])}"

    before_asserts, after_asserts = _assert_count(before), _assert_count(after)
    if after_asserts < before_asserts:
        return f"assertion count fell from {before_asserts} to {after_asserts}"

    for name, body in after.items():
        if not _TEST_PATH.search(name):
            continue
        if _WEAKENER.search(body) and not _WEAKENER.search(before.get(name, "")):
            return f"added a skip/xfail marker to {name}"

    if "requirements.txt" in before and "requirements.txt" not in after:
        return "deleted requirements.txt"

    return None


async def attempt_repair(
    ctx: FactoryContext,
    *,
    product_id: str,
    workspace: str,
    test_command: str,
    healing: str,
    recalled: str,
    task_id: str | None = None,
    category: str = "",
) -> RepairOutcome:
    """Run one bounded repair agent against the workspace.

    ``category == "growth_seo"`` switches both the role and the objective framing:
    GROWTH_ANALYST's own judgement that a LIVE product would benefit from an
    organic-discoverability pass is not a test failure, and telling SEO_OPTIMIZER
    "the test suite is failing" when it is not would be actively misleading about
    why it is here. Every other category keeps WORKSPACE_SURGEON's original framing.

    Raises:
        RepairBlocked: the agent never got a fair attempt (outage, budget exhausted).
    """
    before = _read_tree(resolve_workspace(workspace, ctx.settings))

    if category == "growth_seo":
        role = get_role(RoleName.SEO_OPTIMIZER)
        objective = (
            "A growth analysis flagged this product for an SEO/organic-"
            "discoverability improvement pass -- there is no failing test.\n\n"
            f"What the analysis found:\n{healing}\n\n"
            f"Verify your change with exactly this command when you are done:\n"
            f"    {test_command}"
        )
    else:
        role = get_role(RoleName.WORKSPACE_SURGEON)
        objective = (
            f"The test suite for this product is failing. Fix it.\n\n"
            f"Run the suite with exactly this command:\n    {test_command}\n\n"
            f"What the last verification run reported:\n{healing}"
        )
    # The knowledge file goes first: it is what lets this run start from "here is how
    # this product is built" instead of spending steps rediscovering it, and it is
    # the whole reason a repair on a months-old product should be no more expensive
    # than one on a fresh build.
    knowledge = read_knowledge(workspace, ctx.settings)
    context = "\n\n".join(part for part in (knowledge, recalled) if part.strip())

    runtime = AgentRuntime(
        ctx.llm,
        settings=ctx.settings,
        tools=build_workspace_tools(
            workspace=workspace,
            settings=ctx.settings,
            sandbox=ctx.sandbox,
            test_command=test_command,
        ),
    )
    result = await runtime.run(
        objective=objective,
        system=role.system_prompt,
        tool_categories=list(role.tool_categories),
        task_type=role.task_type,
        engine=EngineName.DEVELOPMENT,
        task_id=task_id,
        max_steps=role.max_steps,
        deadline_seconds=role.deadline_seconds,
        docker_available=ctx.docker_available,
        context=context,
        max_result_chars=MAX_RESULT_CHARS,
        cost_cap_usd=ctx.settings.repair_agent_cost_cap_usd,
        cost_model=model_for(role.task_type, ctx.settings),
        max_context_chars=role.max_context_chars,
    )

    if result.stopped_because in BLOCKED_REASONS:
        raise RepairBlocked(
            f"repair agent stopped early ({result.stopped_because}) after {result.steps} step(s)"
        )

    after = _read_tree(resolve_workspace(workspace, ctx.settings))
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}

    # An answer alone proves nothing: "completed" means only that the model emitted a
    # turn with no tool calls, so a confident prose diagnosis that never touched a
    # file reports success just as loudly as a real fix would.
    if not result.invocations or not changed:
        return RepairOutcome(
            changed_files=set(),
            answer=result.answer,
            summary=result.summary(),
            rejected_because="the agent changed nothing",
        )

    violation = audit_edits(before, after)
    if violation is not None:
        log.warning("repair.rejected", product_id=product_id, reason=violation)
        return RepairOutcome(
            changed_files=changed,
            answer=result.answer,
            summary=result.summary(),
            rejected_because=violation,
        )

    log.info(
        "repair.applied",
        product_id=product_id,
        files=sorted(changed)[:10],
        steps=result.steps,
    )
    return RepairOutcome(changed_files=changed, answer=result.answer, summary=result.summary())
