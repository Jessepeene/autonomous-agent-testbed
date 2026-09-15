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


class UnknownWorkspace(ValueError):
    """A safe name, but no workspace by it exists.

    Distinct from `UnsafeWorkspace` because the two mean opposite things to the
    caller: that one is an attempt to leave the sandbox, this one is a name that was
    guessed. Conflating them would report an invented workspace as a security event
    and bury the thing that actually needs saying -- use the name you were given.
    """


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
#: could otherwise fix it, so nothing ambiguous is on this list -- these specific
#: phrases are the exception (see `_SHELL_DIAGNOSTIC_PHRASES` below), because they
#: routinely appear in a generated test's *own* assertion text (e.g. a permissions
#: feature under test literally asserting `== "Permission denied"`), not just in a
#: real shell/OS diagnostic.
_DEVOPS_ERROR_SIGNATURES: tuple[str, ...] = (
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

#: "permission denied" / "operation not permitted" are only trustworthy as a devops
#: signal when they follow a real shell/OS diagnostic prefix -- `<command>: ...:
#: <message>`, e.g. `chmod: changing permissions of 'x': Operation not permitted` or
#: `sh: 1: foo: Permission denied`. A bare substring match (the previous behaviour)
#: also matches those same words appearing anywhere in a generated test's own
#: assertion output (`assert detail == "Permission denied"`), misrouting a real
#: application bug into the devops-escalation path instead of a heal cycle.
_SHELL_DIAGNOSTIC_PATTERN = re.compile(
    r"^\s*\S+(?::\s*\S+)?:\s.*\b(?:permission denied|operation not permitted)\b",
    re.IGNORECASE | re.MULTILINE,
)

#: POSIX exit codes that are conventionally infra-level, never an application
#: assertion failure: 126 (found but not executable), 127 (command not found), 139
#: (SIGSEGV). 137 (SIGKILL) is deliberately *not* here: both call sites
#: (`development/graph.py::_verify_and_ship`) already check `result.timed_out` and
#: return via a dedicated heal category *before* `is_devops_error` is ever
#: consulted, so a 137 that reaches this function was never our own timeout kill --
#: it is almost always the sandbox's own `--memory` cgroup OOM-killing a process
#: *inside* the container, i.e. the generated app's own unbounded memory growth.
#: That is exactly the compute-burn defect the security reviewer hunts for, an
#: application bug, not infrastructure.
_DEVOPS_EXIT_CODES: frozenset[int] = frozenset({126, 127, 139})


def is_devops_error(result: SandboxResult) -> bool:
    """True when a sandbox failure looks like a host/environment problem rather than
    a bug in the generated application code."""
    if result.exit_code in _DEVOPS_EXIT_CODES:
        return True
    haystack = f"{result.stderr}\n{result.stdout}"
    if _SHELL_DIAGNOSTIC_PATTERN.search(haystack):
        return True
    lowered = haystack.lower()
    return any(signature in lowered for signature in _DEVOPS_ERROR_SIGNATURES)


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


def resolve_workspace(
    workspace: str, settings: Settings | None = None, *, create: bool = False
) -> Path:
    """Map a workspace name to a directory under the workspace root.

    Rejects anything that is not a plain name, so ``../`` or an absolute path cannot
    mount the host filesystem into a container.

    **Creating is explicit, and the default is not to.** This used to `mkdir` on
    every call, which meant a workspace name an agent invented became a real empty
    directory the moment it was mentioned -- so a command run against a guess
    succeeded, returned nothing useful, and taught the agent nothing. It then guessed
    again. Production accumulated 15 such directories against 2 real products,
    including one literally named `nonexistent-xyz-123`, and an agent was observed
    burning steps on `find / -maxdepth 3 -type d -name '*workspace*'` looking for the
    workspace it had been told it had.

    Failing loudly is the useful answer: "there is no workspace called 'api'" is true
    and actionable, where an empty directory is a plausible lie. Only the two places
    that genuinely bring a workspace into existence pass ``create=True``.
    """
    settings = settings or get_settings()
    if not _SAFE_WORKSPACE.match(workspace):
        raise UnsafeWorkspace(f"invalid workspace name: {workspace!r}")
    root = settings.workspace_root.resolve()
    path = (root / workspace).resolve()
    if path != root and root not in path.parents:
        raise UnsafeWorkspace(f"workspace {workspace!r} escapes {root}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    elif not path.is_dir():
        raise UnknownWorkspace(
            f"there is no workspace called {workspace!r}. It was never created, so "
            "nothing can be read from or run in it. If you were given a workspace "
            "name, use exactly that name; do not guess at one."
        )
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
