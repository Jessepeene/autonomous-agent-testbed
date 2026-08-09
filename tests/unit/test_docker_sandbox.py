"""Docker sandbox hardening.

No Docker daemon runs in this environment (or in CI), so these tests never invoke
the real ``docker`` binary. Instead they assert on the exact argv the sandbox would
execute — which is precisely the thing that matters: code written by a model, never
reviewed by a person, is one flag away from escaping its container. `execute()`'s
subprocess call is mocked to check the failure paths (timeout, exit code) without a
daemon.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from factory.sandbox.docker_runner import DockerSandbox
from factory.sandbox.runner import UnsafeWorkspace
from factory.settings import Settings


def _flag_value(argv: list[str], flag: str) -> str:
    """The value immediately following a `--flag value` pair."""
    return argv[argv.index(flag) + 1]


@pytest.fixture
def sandbox(settings: Settings) -> DockerSandbox:
    return DockerSandbox(settings)


class TestArgvHardening:
    """Every flag here closes a specific escape or resource-exhaustion path."""

    def test_network_is_none_by_default(self, sandbox: DockerSandbox) -> None:
        """Generated code cannot phone home or exfiltrate unless a run explicitly
        opts into egress."""
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=False, env=None)
        assert _flag_value(argv, "--network") == "none"

    def test_network_can_be_opted_into_explicitly(self, sandbox: DockerSandbox) -> None:
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=True, env=None)
        assert _flag_value(argv, "--network") == "bridge"

    def test_all_capabilities_are_dropped(self, sandbox: DockerSandbox) -> None:
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=False, env=None)
        assert _flag_value(argv, "--cap-drop") == "ALL"

    def test_privilege_escalation_is_blocked(self, sandbox: DockerSandbox) -> None:
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=False, env=None)
        assert _flag_value(argv, "--security-opt") == "no-new-privileges"

    def test_root_filesystem_is_read_only(self, sandbox: DockerSandbox) -> None:
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=False, env=None)
        assert "--read-only" in argv

    def test_container_runs_as_a_non_root_uid(self, sandbox: DockerSandbox) -> None:
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=False, env=None)
        assert _flag_value(argv, "--user") == "10001:10001"

    def test_container_is_removed_after_exit(self, sandbox: DockerSandbox) -> None:
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=False, env=None)
        assert "--rm" in argv

    def test_container_never_holds_a_stdin_tty(self, sandbox: DockerSandbox) -> None:
        """No `-i`/`-t`: a container waiting on stdin is a container that never exits."""
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=False, env=None)
        assert "--interactive=false" in argv
        assert "-it" not in argv
        assert "-i" not in argv
        assert "-t" not in argv

    def test_resource_limits_come_from_settings(self, settings: Settings) -> None:
        custom = settings.model_copy(
            update={"sandbox_pids_limit": 64, "sandbox_memory_mb": 512, "sandbox_cpus": 0.5}
        )
        argv = DockerSandbox(custom)._argv(
            "echo hi", workspace_path="/tmp/ws", network=False, env=None
        )
        assert _flag_value(argv, "--pids-limit") == "64"
        assert _flag_value(argv, "--memory") == "512m"
        assert _flag_value(argv, "--memory-swap") == "512m"
        assert _flag_value(argv, "--cpus") == "0.5"

    def test_never_privileged_and_never_mounts_the_docker_socket(
        self, sandbox: DockerSandbox
    ) -> None:
        """These are exactly the two patterns the Bug Council's privilege scanner
        blocks in generated code; the sandbox launching that code must not carry
        them either."""
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=False, env=None)
        rendered = " ".join(argv)
        assert "--privileged" not in rendered
        assert "docker.sock" not in rendered
        assert "--cap-add" not in rendered

    def test_workspace_is_the_only_bind_mount(self, sandbox: DockerSandbox) -> None:
        argv = sandbox._argv(
            "echo hi", workspace_path="/host/workspaces/product-1", network=False, env=None
        )
        volumes = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--volume"]
        assert volumes == ["/host/workspaces/product-1:/workspace:rw"]

    def test_env_vars_are_passed_individually_and_only_those(self, sandbox: DockerSandbox) -> None:
        argv = sandbox._argv(
            "echo hi",
            workspace_path="/tmp/ws",
            network=False,
            env={"STRIPE_KEY": "sk_test_x", "OTHER": "value"},
        )
        env_pairs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--env"]
        assert set(env_pairs) == {"STRIPE_KEY=sk_test_x", "OTHER=value"}

    def test_no_env_flags_when_none_are_given(self, sandbox: DockerSandbox) -> None:
        argv = sandbox._argv("echo hi", workspace_path="/tmp/ws", network=False, env=None)
        assert "--env" not in argv

    def test_command_runs_through_a_shell_inside_the_named_image(self, settings: Settings) -> None:
        argv = DockerSandbox(settings)._argv(
            "pytest -q", workspace_path="/tmp/ws", network=False, env=None
        )
        assert argv[-3:] == [settings.sandbox_image, "sh", "-c", "pytest -q"][-3:]
        assert argv[-1] == "pytest -q"


class TestExecute:
    """`execute()` without a real daemon: the subprocess call is mocked."""

    @pytest.mark.parametrize("bad_name", ["../escape", "/etc", ".."])
    async def test_unsafe_workspace_is_rejected_before_any_subprocess_runs(
        self, sandbox: DockerSandbox, bad_name: str
    ) -> None:
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            with pytest.raises(UnsafeWorkspace):
                await sandbox.execute("echo hi", workspace=bad_name)
        mock_exec.assert_not_called()

    async def test_successful_run_reports_stdout_and_exit_code(
        self, sandbox: DockerSandbox
    ) -> None:
        process = AsyncMock()
        process.communicate = AsyncMock(return_value=(b"hello\n", b""))
        process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            result = await sandbox.execute("echo hello", workspace="product-1")

        assert result.ok is True
        assert result.exit_code == 0
        assert result.stdout == "hello\n"
        assert result.timed_out is False

    async def test_nonzero_exit_is_reported_but_not_raised(self, sandbox: DockerSandbox) -> None:
        process = AsyncMock()
        process.communicate = AsyncMock(return_value=(b"", b"boom\n"))
        process.returncode = 1

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            result = await sandbox.execute("false", workspace="product-1")

        assert result.ok is False
        assert result.exit_code == 1
        assert result.stderr == "boom\n"

    async def test_timeout_kills_the_process_and_reports_124(self, sandbox: DockerSandbox) -> None:
        """A runaway container must be killed, not left to burn its resource limits
        until something else notices. Exercises the real `asyncio.wait_for` — only
        the process object and subprocess creation are mocked — so `communicate()`
        raising TimeoutError when awaited is what actually trips the sandbox's
        except clause, the same as a real hang would."""
        process = MagicMock()

        async def hangs_forever(*args: Any, **kwargs: Any) -> tuple[bytes, bytes]:
            raise TimeoutError

        process.communicate = hangs_forever
        # Process.kill() is synchronous in asyncio's API (it sends a signal and
        # returns immediately); only wait() is a coroutine.
        process.kill = MagicMock()
        process.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            result = await sandbox.execute("sleep 999", workspace="product-1")

        process.kill.assert_called_once()
        assert result.timed_out is True
        assert result.exit_code == 124

    async def test_available_is_always_true_for_the_docker_backend(
        self, sandbox: DockerSandbox
    ) -> None:
        """`available` here means "this backend, if selected, will attempt to run
        real containers" — selection between this and `DisabledSandbox` happens in
        `get_sandbox()`, based on whether a daemon actually answers."""
        assert sandbox.available is True
