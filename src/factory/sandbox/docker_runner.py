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
import uuid

from factory.logging import get_logger
from factory.sandbox.runner import SandboxResult, resolve_workspace
from factory.settings import Settings, get_settings

log = get_logger(__name__)

_KILL_GRACE_SECONDS = 5
#: A generated test/app that logs pathologically must not be able to buffer
#: arbitrarily large output in the *factory's own* process memory --
#: `run_in_sandbox`'s tool result is truncated to 20_000 chars anyway, so nothing
#: past a generous multiple of that is ever used. Read-and-discard past this point
#: (see `_read_capped`), not a hard stop -- the pipe must stay drained or the child
#: blocks on a full pipe buffer instead of actually running.
_MAX_OUTPUT_BYTES = 1_000_000
_READ_CHUNK_BYTES = 65_536


async def _read_capped(stream: asyncio.StreamReader | None, cap: int) -> bytes:
    """Drain a stream to EOF, keeping only the first ``cap`` bytes.

    Draining past the cap (rather than stopping) is required: an undrained pipe
    fills its OS buffer and blocks the child process's next write, silently
    turning a chatty process into a hung one.
    """
    if stream is None:
        return b""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        if total < cap:
            room = cap - total
            chunks.append(chunk[:room])
        total += len(chunk)
    return b"".join(chunks)


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
        name: str = "factory-sandbox",
    ) -> list[str]:
        settings = self._settings
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
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
        # A unique, discoverable name: `process.kill()` on timeout only kills the
        # local `docker run` CLI process, not the daemon-side container it started
        # (which keeps running, still consuming its --memory/--cpus slice) --
        # without a name there was also no way to find and clean up the orphan
        # afterward. See the TimeoutError branch below.
        container_name = f"factory-{uuid.uuid4().hex[:12]}"
        argv = self._argv(
            command, workspace_path=str(path), network=network, env=env, name=container_name
        )

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
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                asyncio.gather(
                    _read_capped(process.stdout, _MAX_OUTPUT_BYTES),
                    _read_capped(process.stderr, _MAX_OUTPUT_BYTES),
                ),
                timeout=timeout,
            )
            await process.wait()
        except TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_SECONDS)
            except TimeoutError:  # pragma: no cover - the daemon is wedged
                log.error("sandbox.kill_failed", workspace=workspace)
            # The container itself is daemon-side and outlives the CLI process
            # `process.kill()` just killed -- stop it explicitly by the name we
            # gave it above. `docker run --rm` only removes it once it *exits*,
            # which a runaway loop never does on its own.
            await self._kill_container(container_name, workspace)
            log.warning("sandbox.timeout", workspace=workspace, timeout_s=timeout)
            return SandboxResult(
                exit_code=124,
                stdout="",
                stderr=f"sandbox exceeded {timeout}s and was killed",
                timed_out=True,
            )

        return SandboxResult(
            exit_code=process.returncode or 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )

    async def _kill_container(self, name: str, workspace: str) -> None:
        try:
            kill_process = await asyncio.create_subprocess_exec(
                "docker",
                "kill",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(kill_process.wait(), timeout=_KILL_GRACE_SECONDS)
        except (TimeoutError, OSError) as exc:
            # Best-effort: the container is orphaned either way (--rm removes it
            # once it exits, whether that's from this kill or the daemon noticing
            # its parent CLI died), and the run itself has already been reported
            # as timed out above regardless of whether this cleanup succeeds.
            log.warning(
                "sandbox.container_kill_failed",
                workspace=workspace,
                container=name,
                detail=str(exc)[:200],
            )
