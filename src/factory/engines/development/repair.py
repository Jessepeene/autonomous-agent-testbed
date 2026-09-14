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

from factory.agents.roles import RoleName, categories_for, get_role
from factory.agents.runtime import AgentRuntime
from factory.engines.development.scaffold.generator import WORKSPACE_SKIP_DIRS
from factory.llm.router import model_for
from factory.logging import get_logger
from factory.sandbox.runner import resolve_workspace
from factory.settings import EngineName, Settings
from factory.supervisor.context import FactoryContext
from factory.tools.handbook import build_handbook_tools
from factory.tools.registry import ToolRegistry
from factory.tools.review import build_review_tools
from factory.tools.workspace import build_workspace_tools

log = get_logger(__name__)

#: Tool results are the bulk of an agent transcript and the transcript is re-sent
#: every step, so this is the single biggest lever on cost.
#:
#: This was 6_000, chosen against *pytest output*, where `_truncate_middle` keeping
#: the head and tail is exactly right -- the failure is at the ends. But the same
#: cap applies to `read_workspace_file`, where middle-elision is fatal rather than
#: lossy, and 6_000 is smaller than a real generated source file.
#:
#: The consequence was not a slightly worse repair, it was no repair at all. A
#: product's `app/main.py` is 547 lines / 20_277 chars; the agent asked for it,
#: received 30% of it with the middle removed, and fell back to paging the file
#: with `sed -n '1,130p'`, `'130,280p'`, `'280,430p'`... burning its entire step
#: budget reconstructing a file it could not read in one call, and writing nothing.
#: Three separate live repairs failed this way before the tool-argument log made it
#: visible -- the tool-name log showed only `run_workspace_command x20`.
#:
#: Sized to hold a whole real source file with headroom. Costs more per step, and
#: is still far cheaper than the previous behaviour: a budget spent entirely on
#: re-reading, producing nothing, every time. `repair_agent_cost_cap_usd` remains
#: the real economic bound, and `max_context_chars` compacts older results.
#:
#: The better fix is a per-tool result budget -- a pytest dump and a source file
#: genuinely want different limits -- but `AgentRuntime.run` takes one cap for the
#: whole run, so that is a runtime change rather than a constant.
#: Fallback only. The live value is derived per run from the role's own transcript
#: budget -- see `_result_budget`.
MAX_RESULT_CHARS = 24_000


def _result_budget(role_max_context_chars: int | None) -> int:
    """How much one tool result may occupy, derived rather than chosen.

    A flat constant was the wrong shape and outgrew itself twice. It was 6_000
    against a 20_277-char source file; raised to 24_000; and the file is now 34_604
    because every regeneration adds code, so the same failure returned -- four
    consecutive repair attempts spending their whole budget re-reading one file with
    `sed` and changing nothing.

    The real constraint was never per-result size. `AgentRuntime._compact_to_budget`
    already bounds the *transcript* and does it non-destructively: it evicts the
    oldest results, which an agent can read again. Truncating each result as it
    arrives solves a problem that is already solved, in the one place where it does
    permanent damage.

    Half the transcript budget, because the only genuine constraint on a single
    result is that it must not crowd out everything else -- past half, one read
    evicts essentially all context and the run is worse off than if it had paged.
    Not a number picked for a file size, so it cannot be outgrown by one.
    """
    return max(MAX_RESULT_CHARS, (role_max_context_chars or 0) // 2)

#: Outcomes that mean the agent genuinely never got to run: something in the
#: environment stopped it before it could work on the problem. Deferring costs
#: nothing and the next attempt may well succeed, so these must not consume a heal
#: cycle -- see `RepairBlocked`.
BLOCKED_REASONS = frozenset(
    {
        "no_provider",
        "provider_error",
        "not_permitted",
    }
)

#: Outcomes that mean the agent ran, worked, and used up its entire budget without
#: finishing. Deliberately NOT in BLOCKED_REASONS despite being listed there
#: originally: deferring these produces an infinite loop, because the next attempt
#: starts from the same rolled-back workspace with the same budget and gets the same
#: result. Observed live -- one real patch reached 43 attempts over nine hours,
#: repeatedly spending a full agent run to hit `step_limit` at 8 steps and roll back,
#: never counting a heal cycle and so never escalating to a human.
#:
#: These consume a heal cycle instead, so a repair the surgical path cannot do
#: within budget eventually escalates as HEAL_EXHAUSTED -- which is the honest
#: outcome: it needs a bigger tool or a person, not a 44th identical try.
EXHAUSTED_REASONS = frozenset(
    {
        "step_limit",
        "cost_cap",
        "deadline",
        "empty_response",
        "truncated",
    }
)

#: Step budget for a `guided` repair, overriding WORKSPACE_SURGEON's default 8.
#:
#: The two categories are not the same shape of problem. A `test_failure` arrives
#: with a traceback naming a file and a line, so the work is read-one-file, edit,
#: re-run: comfortably inside 8. A `guided` repair arrives as a *symptom* observed
#: from outside -- "clicking Subscribe returns 422 and the UI prints [object
#: Object]" -- with no location at all. Finding it means listing the tree, reading
#: the frontend call site, the backend route, and the request model before a single
#: edit, which spends the entire 8-step budget on reading.
#:
#: Confirmed live rather than reasoned about: a real 422-on-checkout repair was
#: attempted repeatedly and the workspace was never modified once -- every run hit
#: `step_limit` and was rolled back. The role docstring's "past ~8 steps a surgical
#: repair stops being cheaper than the regeneration it replaces" is the right
#: instinct but the wrong lever here: `repair_agent_cost_cap_usd` ($0.75, set
#: deliberately below a full regeneration's ~$1.15) already enforces that economic
#: bound directly, and now consumes a heal cycle when hit rather than looping. The
#: step count was only ever a proxy for cost, and a poor one on a cheap model.
GUIDED_MAX_STEPS = 16

_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*\.py$|[^/]*_test\.py$)")
_ASSERT = re.compile(r"\bassert\b")
_WEAKENER = re.compile(r"@pytest\.mark\.(skip|xfail)\b|pytest\.(skip|xfail)\(")



def _workspace_and_review_tools(
    ctx: FactoryContext, workspace: str, test_command: str, *, max_file_chars: int
) -> ToolRegistry:
    """The engineer's own toolkit: edit the workspace, and check the work before
    declaring it done.

    Bug Council still gates the deploy -- that rail does not move. What this adds is
    the half a human team has always had alongside CI: a developer running the
    linter locally before pushing. Without it the loop was repair, finish, gate,
    findings, new heal cycle, repeatedly trading one set of blockers for another.
    """
    registry = ToolRegistry.extending(
        build_workspace_tools(
            workspace=workspace,
            settings=ctx.settings,
            sandbox=ctx.sandbox,
            test_command=test_command,
            max_file_chars=max_file_chars,
        )
    )
    for descriptor in build_review_tools(ctx, workspace=workspace).all():
        registry.register(descriptor)
    return registry


class RepairBlocked(RuntimeError):
    """The repair was never actually attempted -- the environment stopped it first.

    A provider outage or a refused spawn says nothing about whether the code is
    fixable, so the caller defers the task rather than burning one of its three
    heal cycles. Mirrors `CampaignApprovalPending`, which `supervisor/graph.py`
    already defers for the same reason.

    Note what is deliberately *not* here any more: an exhausted step or cost
    budget. That is an agent which ran, worked, and did not finish -- retrying it
    unchanged is guaranteed to reach the same place, so it is a real failed attempt
    (see EXHAUSTED_REASONS), not a blocked one.
    """


@dataclass(slots=True)
class RepairOutcome:
    changed_files: set[str]
    answer: str
    summary: str
    rejected_because: str | None = None
    #: The agent investigated and concluded the brief should not be actioned.
    #:
    #: Its own words, from the run that forced this to exist: "Made NO code change.
    #: Investigated the reviewer guidance and concluded it must not be actioned,
    #: because both halves are either already satisfied or directly forbidden by
    #: this product's own acceptance criteria." It had verified the claims, found
    #: them false, and refused to evade an assertion to satisfy them.
    #:
    #: That was scored `repair_unusable: the agent changed nothing` and burned a
    #: heal cycle, because usability was `bool(changed_files)` -- so a reasoned
    #: refusal was indistinguishable from an agent that tried and failed. The same
    #: shape as RESULT: BLOCKED being mandated by the rules and read by nothing.
    #:
    #: A brief that turns out to be wrong is a *finding*, and the loop should end
    #: there rather than spend its remaining budget trying to make it true.
    no_change_needed: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.changed_files) and self.rejected_because is None

    @property
    def concluded(self) -> bool:
        """The run reached a real conclusion -- either work, or a reasoned refusal."""
        return self.usable or self.no_change_needed


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

    ``category`` switches both the role and the objective framing wherever the
    generic "the test suite is failing" story would be actively false: telling
    an agent it's fixing a failing test when it is not risks it hunting for a
    problem that does not exist, or misreading real guidance as a test
    traceback. ``growth_seo`` was the first case (GROWTH_ANALYST's own
    judgement call, not a failure at all); ``council_rejection:*`` and
    ``guided`` are the same shape of gap -- by the time either fires, the test
    suite has already passed. Every other category keeps WORKSPACE_SURGEON's
    original test-failure framing.

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
    elif category.startswith("council_rejection:"):
        role = get_role(RoleName.WORKSPACE_SURGEON)
        objective = (
            "The test suite already passes. Bug Council reviewed this product "
            "afterward and did not approve it -- there is no failing test to "
            "chase; fix exactly what Council found.\n\n"
            f"What Council found:\n{healing}\n\n"
            f"Verify your change with exactly this command when you are done:\n"
            f"    {test_command}"
        )
    elif category == "guided":
        role = get_role(RoleName.WORKSPACE_SURGEON)
        objective = (
            "The test suite already passes. A reviewer looked at this product and "
            "gave you specific guidance -- there is no failing test to chase; act "
            "on exactly what they said. The guidance below states where it was "
            "observed.\n\n"
            f"Their guidance:\n{healing}\n\n"
            "If it was observed on the running deployment, two things follow. The "
            "workspace can be *ahead* of what is live -- an earlier fix may be "
            "sitting here unshipped -- so source that already looks correct does "
            "not mean the report is stale, and it is not evidence the bug is gone. "
            "And you cannot reproduce it by running the suite; reason from the code "
            "path the report names instead. Do not go looking for the deployed "
            "build: you cannot see it from here, and nothing in this workspace "
            "is a reliable proxy for it.\n\n"
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
    # One budget, derived from this role's own transcript ceiling and used for both
    # the tool's page size and the runtime's result cap -- so a file the tool hands
    # back whole is never mutilated a second time on the way into the transcript.
    result_budget = _result_budget(role.max_context_chars)

    runtime = AgentRuntime(
        ctx.llm,
        settings=ctx.settings,
        # Layered, not replaced: the workspace tools are this role's whole job, and
        # the handbook is what lets it know what the product it is editing is *for*
        # -- and read the repairs already tried on it, which is the difference
        # between a second attempt and a repeat of the first.
        tools=build_handbook_tools(
            role=role.name.value,
            task_id=task_id,
            product_id=product_id,
            engine=EngineName.DEVELOPMENT,
            base=_workspace_and_review_tools(
                ctx, workspace, test_command, max_file_chars=result_budget
            ),
        ),
    )
    result = await runtime.run(
        objective=objective,
        system=role.system_prompt,
        tool_categories=categories_for(role),
        task_type=role.task_type,
        engine=EngineName.DEVELOPMENT,
        task_id=task_id,
        max_steps=GUIDED_MAX_STEPS if category == "guided" else role.max_steps,
        deadline_seconds=role.deadline_seconds,
        docker_available=ctx.docker_available,
        context=context,
        max_result_chars=result_budget,
        cost_cap_usd=ctx.settings.repair_agent_cost_cap_usd,
        cost_model=model_for(role.task_type, ctx.settings),
        max_context_chars=role.max_context_chars,
    )

    if result.stopped_because in BLOCKED_REASONS:
        raise RepairBlocked(
            f"repair agent stopped early ({result.stopped_because}) after {result.steps} step(s)"
        )
    if result.stopped_because in EXHAUSTED_REASONS:
        # Ran and spent its whole budget without finishing. Consumes a heal cycle
        # rather than being raised as RepairBlocked -- see EXHAUSTED_REASONS for the
        # 43-attempt loop that prevents.
        #
        # But "the agent ran out of budget" is not the same as "the work is
        # unusable", and the first version of this conflated them: it returned
        # `changed_files=set()` unconditionally, so any edit a capped run had
        # already made was discarded before anything looked at it. The gate that
        # actually decides whether a repair ships is `_verify_and_ship` -- the test
        # suite and Bug Council -- not whether the agent felt finished. A capped run
        # whose edits pass both is a good repair that happened to be slow; throwing
        # it away guarantees the next cycle starts from nothing and hits the same
        # ceiling.
        #
        # So the edits are kept and allowed to face verification. A half-finished
        # edit is not a risk here: it fails the suite or Council and is rolled back
        # by the caller exactly as before.
        after_exhausted = _read_tree(resolve_workspace(workspace, ctx.settings))
        changed_when_capped = {
            name
            for name in set(before) | set(after_exhausted)
            if before.get(name) != after_exhausted.get(name)
        }
        log.info(
            "repair.budget_exhausted",
            product_id=product_id,
            stopped=result.stopped_because,
            steps=result.steps,
            changed_files=len(changed_when_capped),
            # What the agent said about its own run, which `_REPAIR_RULES` has
            # always demanded and nothing has ever read. It stays advisory -- the
            # file diff above decides whether work happened -- but it is the only
            # thing that can say *why* not, and three separate runs reported a
            # deleted toolchain into the void before this was wired up.
            reported=result.reported_result or "none",
            reported_detail=result.reported_detail[:200],
        )
        if not changed_when_capped:
            return RepairOutcome(
                changed_files=set(),
                answer=result.answer,
                summary=result.summary(),
                rejected_because=(
                    f"the repair agent used its entire budget without changing "
                    f"anything ({result.stopped_because} after {result.steps} step(s))"
                ),
            )
        violation = audit_edits(before, after_exhausted)
        if violation is not None:
            log.warning("repair.rejected", product_id=product_id, reason=violation)
            return RepairOutcome(
                changed_files=set(),
                answer=result.answer,
                summary=result.summary(),
                rejected_because=violation,
            )
        return RepairOutcome(
            changed_files=changed_when_capped,
            answer=result.answer,
            summary=result.summary(),
        )

    after = _read_tree(resolve_workspace(workspace, ctx.settings))
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}

    # An answer alone proves nothing: "completed" means only that the model emitted a
    # turn with no tool calls, so a confident prose diagnosis that never touched a
    # file reports success just as loudly as a real fix would.
    if not result.invocations or not changed:
        # "I investigated and nothing should change" is only credible from an agent
        # that actually investigated -- `invocations` is the evidence, the same bar
        # `spawner.py` uses before trusting an answer enough to remember it.
        if result.invocations and result.reported_result == "NOT_NEEDED":
            log.info(
                "repair.no_change_needed",
                product_id=product_id,
                steps=result.steps,
                detail=result.reported_detail[:200],
            )
            return RepairOutcome(
                changed_files=set(),
                answer=result.answer,
                summary=result.summary(),
                no_change_needed=True,
            )
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
