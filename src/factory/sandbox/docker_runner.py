"""Ephemeral Docker sandbox.

Every hardening flag here exists because the code being run was written by a model
and never reviewed by a person:

* ``--network none`` by default — generated code cannot phone home or exfiltrate.
* ``--cap-drop ALL`` + ``--security-opt no-new-privileges`` — no privilege gain.
* ``--read-only`` root filesystem with a writable tmpfs and one bind-mounted
  workspace — nothing persists outside the product's own directory.
* ``--user`` non-root, ``--pids-limit``, ``--memory``, ``--cpus`` — a runaway loop
  burns its own container's budget and then dies, not the host.
* ``--rm`` — the container is gone whatever happens.

Secrets are passed as run-scoped environment variables and never written into an
image layer.
"""

from __future__ import annotations

import asyncio
import shlex

from factory.logging import get_logger
from factory.sandbox.runner import SandboxResult, resolve_workspace
from factory.settings import Settings, get_settings

log = get_logger(__name__)

_KILL_GRACE_SECONDS = 5


class DockerSandbox:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return True

    def _argv(
        self,
        command: str,
        *,
        workspace_path: str,
        network: bool,
        env: dict[str, str] | None,
    ) -> list[str]:
        settings = self._settings
        argv = [
            "docker",
            "run",
            "--rm",
            "--interactive=false",
            "--network",
            "bridge" if network else "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--read-only",
            "--tmpfs",
            # Path inside the ephemeral container, not on the host filesystem.
            "/tmp:rw,noexec,nosuid,size=256m",  # noqa: S108
            "--user",
            "10001:10001",
            "--pids-limit",
            str(settings.sandbox_pids_limit),
            "--memory",
            f"{settings.sandbox_memory_mb}m",
            "--memory-swap",
            f"{settings.sandbox_memory_mb}m",
            "--cpus",
            str(settings.sandbox_cpus),
            "--workdir",
            "/workspace",
            "--volume",
            f"{workspace_path}:/workspace:rw",
            # uid 10001 has no /etc/passwd entry in the sandbox image, so $HOME is
            # unset. Any tool that resolves a cache/config dir from $HOME/... (pip's
            # build isolation, and anything it shells out to -- observed live: a
            # dependency needing a from-source build tried to write its build tool's
            # cache to the literal path "/.cache") then hits the read-only rootfs
            # instead of the writable tmpfs above, which is only mounted at /tmp.
            # Pointing HOME at /tmp covers the whole class of "wants a dotfile-style
            # cache under home" failures in one shot, rather than enumerating and
            # separately tmpfs-mounting every path a build tool might invent.
            "--env",
            "HOME=/tmp",
        ]
        for key, value in (env or {}).items():
            argv += ["--env", f"{key}={value}"]
        argv += [settings.sandbox_image, "sh", "-c", command]
        return argv

    async def execute(
        self,
        command: str,
        *,
        workspace: str,
        timeout_seconds: int | None = None,
        network: bool = False,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        settings = self._settings
        path = resolve_workspace(workspace, settings)
        timeout = timeout_seconds or settings.sandbox_timeout_seconds
        argv = self._argv(command, workspace_path=str(path), network=network, env=env)

        log.info(
            "sandbox.exec",
            workspace=workspace,
            network=network,
            timeout_s=timeout,
            command=shlex.split(command)[:1],
        )

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_SECONDS)
            except TimeoutError:  # pragma: no cover - the daemon is wedged
                log.error("sandbox.kill_failed", workspace=workspace)
            log.warning("sandbox.timeout", workspace=workspace, timeout_s=timeout)
            return SandboxResult(
                exit_code=124,
                stdout="",
                stderr=f"sandbox exceeded {timeout}s and was killed",
                timed_out=True,
            )

        return SandboxResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
