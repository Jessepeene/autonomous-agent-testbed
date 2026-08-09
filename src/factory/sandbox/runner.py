"""Sandbox interface and selection.

Generated code is untrusted by definition — it was written by a model, from ideas
scraped off the public internet, and nobody reviewed it before it ran. It therefore
executes in an ephemeral container or it does not execute at all. There is
deliberately no host-execution implementation in this package for a fallback to
reach for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from factory.settings import Settings, get_settings

_SAFE_WORKSPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class SandboxUnavailable(RuntimeError):
    """No sandbox backend is available, so nothing may be executed."""


class UnsafeWorkspace(ValueError):
    """The requested workspace name could escape the workspace root."""


class DevOpsError(RuntimeError):
    """A sandbox failure that looks like a host/environment problem rather than a
    bug in the generated application code — a missing binary, a permission block, a
    network failure reaching a package registry.

    Raised so callers can route it away from an application-logic repair cycle
    (which would spend an LLM call rewriting code that was never the actual problem)
    and toward the host/ops problem it actually is. See
    `development/graph.py::_verify_and_ship` for where this is raised and
    `supervisor/graph.py::_run_engine` for where it is caught.
    """


@dataclass(slots=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


#: stderr/stdout substrings that indicate the *environment*, not the generated code,
#: is at fault. Matched case-insensitively. Deliberately conservative: a false
#: negative here just means a devops failure is treated as before (burns a heal
#: cycle); a false positive would hide a real logic bug from the codegen model that
#: could otherwise fix it, so nothing ambiguous is on this list.
_DEVOPS_ERROR_SIGNATURES: tuple[str, ...] = (
    "permission denied",
    "operation not permitted",
    ": not found",  # POSIX shell's own "sh: 1: foo: not found"
    "command not found",
    "cannot connect to the docker daemon",
    "oci runtime",
    "exec format error",
    "exec user process caused",
    "read-only file system",
    "network is unreachable",
    "temporary failure in name resolution",
    "could not resolve host",
    "no space left on device",
)

#: POSIX exit codes that are conventionally infra-level, never an application
#: assertion failure: 126 (found but not executable), 127 (command not found), 137
#: (SIGKILL — almost always the OOM killer, or our own timeout kill), 139 (SIGSEGV).
_DEVOPS_EXIT_CODES: frozenset[int] = frozenset({126, 127, 137, 139})


def is_devops_error(result: SandboxResult) -> bool:
    """True when a sandbox failure looks like a host/environment problem rather than
    a bug in the generated application code."""
    if result.exit_code in _DEVOPS_EXIT_CODES:
        return True
    haystack = f"{result.stderr}\n{result.stdout}".lower()
    return any(signature in haystack for signature in _DEVOPS_ERROR_SIGNATURES)


class SandboxRunner(Protocol):
    @property
    def available(self) -> bool: ...

    async def execute(
        self,
        command: str,
        *,
        workspace: str,
        timeout_seconds: int | None = None,
        network: bool = False,
        env: dict[str, str] | None = None,
    ) -> SandboxResult: ...


def resolve_workspace(workspace: str, settings: Settings | None = None) -> Path:
    """Map a workspace name to a directory under the workspace root.

    Rejects anything that is not a plain name, so ``../`` or an absolute path cannot
    mount the host filesystem into a container.
    """
    settings = settings or get_settings()
    if not _SAFE_WORKSPACE.match(workspace):
        raise UnsafeWorkspace(f"invalid workspace name: {workspace!r}")
    root = settings.workspace_root.resolve()
    path = (root / workspace).resolve()
    if path != root and root not in path.parents:
        raise UnsafeWorkspace(f"workspace {workspace!r} escapes {root}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_sandbox(settings: Settings | None = None) -> SandboxRunner:
    """Docker when a daemon answers, otherwise a runner that refuses everything."""
    from factory.preflight import docker_available
    from factory.sandbox.disabled import DisabledSandbox
    from factory.sandbox.docker_runner import DockerSandbox

    settings = settings or get_settings()
    if docker_available():
        return DockerSandbox(settings)
    return DisabledSandbox()
