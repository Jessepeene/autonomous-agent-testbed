"""Workspace-surgery tools, built fresh for one repair run.

These are the tools a repair agent uses to fix a product *in place* instead of the
Development Engine regenerating the whole codebase. They differ from
``builtin.run_in_sandbox`` in three deliberate ways, and each one is load-bearing:

**The workspace is closed over, never a model argument.** ``run_in_sandbox`` takes
``workspace`` from the model, and ``resolve_workspace`` *creates* the directory it
resolves (``mkdir(parents=True, exist_ok=True)``) -- so a hallucinated or slightly
wrong name silently produces a new empty directory in which every command "succeeds"
against nothing, and the agent burns its whole budget fixing a product that isn't
there. Here the caller pins it and the model cannot address any other product.

**They do not take ``product_lock``.** ``run_in_sandbox`` acquires it because it is
reachable by an Orchestrator-spawned agent that could collide with a build. These
are only ever constructed by ``development/graph.py::_patch``, which already holds
the lock for its whole body -- so the race is definitionally impossible, and taking
it again would be worse than redundant: ``_patch``'s lock row is still *uncommitted*
in its own transaction, so a second acquisition from another connection does not
fail fast, it **blocks** on the first transaction's outcome until something times
it out.

**File access is host-side, not through the sandbox.** ``write_plan`` already writes
generated files to the workspace directly from this process; only *execution* is
sandboxed. Reading and writing here therefore crosses no boundary that generation
did not already cross, and a structured ``write_workspace_file(path, content)`` is
far more reliable than asking a model to compose correct ``sed``/heredoc syntax
through a shell.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from factory.engines.development.scaffold.generator import WORKSPACE_SKIP_DIRS
from factory.logging import get_logger
from factory.sandbox.runner import SandboxRunner, resolve_workspace
from factory.settings import Settings
from factory.tools.registry import ToolDescriptor, ToolRegistry

log = get_logger(__name__)

#: The most one file read may return in a single call.
#:
#: Not a truncation point -- a *page* boundary. Past this the read returns a bounded
#: chunk that says exactly which lines it covers and how to ask for the rest, so an
#: agent always knows whether it has the whole file.
#:
#: Silent elision is what this replaces, and it was not a small bug: a 34,604-char
#: `app/main.py` came back with its middle removed, the agent correctly distrusted
#: it, and reconstructed the file with seven `sed -n 'X,Yp'` calls instead -- four
#: consecutive repair attempts spent their entire budget re-reading one file and
#: changed nothing. A file has no "signal at the ends" the way a pytest dump does,
#: so middle-elision produces something that looks complete and is not.
MAX_FILE_CHARS = 60_000

#: Enough to see the shape of a generated product (they are ~20 files) without an
#: `ls -R` style dump crowding out the actual task.
MAX_LISTED_FILES = 200


def _safe_relative(root: Path, raw: str) -> Path | None:
    """Resolve a model-supplied relative path, or ``None`` if it escapes the tree.

    ``resolve_workspace`` guards the workspace *name*; nothing guarded the path
    *inside* it, and these tools take one from the model on every call.
    """
    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate


def _is_hidden_infra(path: Path, root: Path) -> bool:
    return any(part in WORKSPACE_SKIP_DIRS for part in path.relative_to(root).parts)


def build_workspace_tools(
    *,
    workspace: str,
    settings: Settings,
    sandbox: SandboxRunner,
    test_command: str,
    max_file_chars: int = MAX_FILE_CHARS,
) -> ToolRegistry:
    """A private registry holding this repair run's four tools.

    Not the process-wide ``REGISTRY``: that is a module-level singleton whose
    ``register()`` raises on a duplicate name, so per-product tools cannot live in
    it. ``AgentRuntime`` already accepts an injected registry for exactly this.
    """
    root = resolve_workspace(workspace, settings)
    registry = ToolRegistry()

    async def list_workspace_files() -> dict[str, Any]:
        files = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or _is_hidden_infra(path, root):
                continue
            files.append(
                {
                    "path": str(path.relative_to(root)).replace("\\", "/"),
                    "bytes": path.stat().st_size,
                }
            )
        truncated = len(files) > MAX_LISTED_FILES
        return {"files": files[:MAX_LISTED_FILES], "truncated": truncated}

    async def read_workspace_file(path: str, offset: int = 0) -> dict[str, Any]:
        target = _safe_relative(root, path)
        if target is None:
            return {"error": f"path {path!r} is outside the workspace"}
        if not target.is_file():
            return {"error": f"{path!r} does not exist"}
        try:
            content = target.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            return {"error": f"{path!r} could not be read as text: {exc}"}

        # Paged by line, because that is the unit the caller thinks in -- the
        # behaviour this replaces was agents reaching for `sed -n 'X,Yp'` when a
        # read came back incomplete.
        lines = content.splitlines(keepends=True)
        total = len(lines)
        start = min(max(offset, 0), total)
        chunk: list[str] = []
        used = 0
        for line in lines[start:]:
            if chunk and used + len(line) > max_file_chars:
                break
            chunk.append(line)
            used += len(line)
        end = start + len(chunk)

        result: dict[str, Any] = {
            "path": path,
            "content": "".join(chunk),
            "lines": f"{start + 1}-{end} of {total}" if total else "empty file",
            "complete": start == 0 and end == total,
        }
        if end < total:
            # Stated plainly and with the exact next call, so "I have the whole
            # file" is never something the agent has to infer.
            result["next_offset"] = end
            result["more"] = (
                f"{total - end} more line(s). Call read_workspace_file with "
                f"offset={end} for the next page."
            )
        return result

    async def write_workspace_file(path: str, content: str) -> dict[str, Any]:
        target = _safe_relative(root, path)
        if target is None:
            return {"error": f"path {path!r} is outside the workspace"}
        if _is_hidden_infra(target, root):
            # `.deps` is the installed-dependency tree the verification step creates;
            # editing a vendored library to make a test pass is never the fix, and
            # nothing under these directories is reviewed or shipped anyway.
            return {"error": f"{path!r} is in an internal directory and must not be edited"}
        if len(content) > MAX_FILE_CHARS:
            return {"error": f"content exceeds {MAX_FILE_CHARS} characters"}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        log.info("workspace.file_written", workspace=workspace, path=path, chars=len(content))
        return {"path": path, "written_chars": len(content)}

    async def run_workspace_command(command: str) -> dict[str, Any]:
        # Byte-identical to how _verify_and_ship runs the suite: same PATH export,
        # same PYTHONPATH. If the agent's reproduce differed from the gate's, it
        # would optimise against a command nobody verifies -- the worst outcome
        # available, because it looks like success right up until the gate runs.
        result = await sandbox.execute(
            f'export PATH="/workspace/.deps/bin:$PATH"; {command}',
            workspace=workspace,
            env={"PYTHONPATH": "/workspace/.deps"},
        )
        return {
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    registry.register(
        ToolDescriptor(
            name="list_workspace_files",
            description=(
                "List every source file in the product workspace, with its size. "
                "Dependencies and build artifacts are excluded. Start here."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            category="workspace",
            handler=list_workspace_files,
        )
    )
    registry.register(
        ToolDescriptor(
            name="read_workspace_file",
            description=(
                "Read one file from the product workspace as text. The result always "
                "states which lines it covers and whether they are the whole file; if "
                "a file is too large for one call it comes back as a page with the "
                "exact offset to request next. Use this rather than shelling out to "
                "sed or head -- it is one call per page and it tells you where you are."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path, e.g. 'app/main.py'.",
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "0-based line to start at. Omit for the start of the file; "
                            "pass the `next_offset` from a previous page to continue."
                        ),
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            category="workspace",
            handler=read_workspace_file,
        )
    )
    registry.register(
        ToolDescriptor(
            name="write_workspace_file",
            description=(
                "Overwrite one file in the product workspace with its full new "
                "content. Always read the file first; this replaces it entirely."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path, e.g. 'app/main.py'.",
                    },
                    "content": {"type": "string", "description": "Complete new file content."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            category="workspace",
            handler=write_workspace_file,
        )
    )
    registry.register(
        ToolDescriptor(
            name="run_workspace_command",
            description=(
                "Run a shell command against the workspace in an isolated container "
                "with no network access. Dependencies are already installed. Use "
                f"this to reproduce the failure and to confirm your fix: {test_command}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."}
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            category="workspace",
            # Not requires_sandbox=True: _patch already refuses the agentic path
            # outright when no sandbox is available, so this would only ever hide the
            # tool from an agent that cannot function without it anyway.
            handler=run_workspace_command,
        )
    )
    return registry
