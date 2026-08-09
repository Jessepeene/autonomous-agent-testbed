"""DevOps-error classification and routing.

Rewriting generated code cannot fix a missing binary, a permission block, or an
unreachable package registry. These tests cover the classifier itself
(`is_devops_error`) and that `_verify_and_ship` actually raises `DevOpsError` — never
calls `_heal()` — for exactly that class of sandbox failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from factory.engines.development import graph as dev_graph
from factory.sandbox.runner import DevOpsError, SandboxResult, is_devops_error
from factory.state import repository
from factory.state.schemas import ProductStatus
from factory.supervisor.context import FactoryContext

# --- is_devops_error: the classifier ------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [
        "bash: pytest: command not found",
        "sh: 1: node: not found",
        "cp: cannot create regular file '/workspace/out.txt': Permission denied",
        "chmod: changing permissions of '/workspace/x': Operation not permitted",
        "docker: Cannot connect to the Docker daemon at unix:///var/run/docker.sock.",
        "standard_init_linux.go:228: exec user process caused: exec format error",
        "OCI runtime exec failed: exec failed: unable to start container process",
        "touch: /workspace/x: Read-only file system",
        "ping: connect: Network is unreachable",
        "curl: (6) Could not resolve host: pypi.org",
        "Temporary failure in name resolution",
        "write /workspace/big: No space left on device",
    ],
)
def test_known_infra_stderr_signatures_are_classified_as_devops(stderr: str) -> None:
    result = SandboxResult(exit_code=1, stdout="", stderr=stderr)
    assert is_devops_error(result) is True


@pytest.mark.parametrize("exit_code", [126, 127, 137, 139])
def test_conventional_infra_exit_codes_are_classified_as_devops(exit_code: int) -> None:
    result = SandboxResult(exit_code=exit_code, stdout="", stderr="")
    assert is_devops_error(result) is True


def test_a_normal_assertion_failure_is_not_classified_as_devops() -> None:
    result = SandboxResult(
        exit_code=1,
        stdout="FAILED tests/test_app.py::test_signup - AssertionError: assert 400 == 201",
        stderr="",
    )
    assert is_devops_error(result) is False


def test_a_successful_result_is_never_classified_as_devops() -> None:
    result = SandboxResult(exit_code=0, stdout="5 passed", stderr="")
    assert is_devops_error(result) is False


def test_a_timed_out_result_with_no_signature_is_not_classified_as_devops() -> None:
    """Timeouts already have their own dedicated handling upstream
    (`install.timed_out` / the sandbox's own timeout path) — the classifier itself
    only looks at exit code and text."""
    result = SandboxResult(exit_code=124, stdout="", stderr="", timed_out=True)
    assert is_devops_error(result) is False


# --- _verify_and_ship: routes a devops failure away from _heal() --------------


@dataclass(slots=True)
class _ScriptedSandbox:
    """Returns queued results in order; records the arguments each call received."""

    results: list[SandboxResult]
    calls: list[dict[str, object]] = field(default_factory=list)
    available: bool = True

    async def execute(
        self,
        command: str,
        *,
        workspace: str,
        timeout_seconds: int | None = None,
        network: bool = False,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        self.calls.append({"command": command, "network": network})
        return self.results.pop(0)


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_verify_and_ship` writes `ProductStatus.REVIEWING` before anything else. The
    only thing under test here is what happens after that write, so it's stubbed
    rather than standing up a real session for one call these tests don't care about.
    """

    async def fake_set_product_status(
        session: object, product_id: str, status: ProductStatus, **fields: object
    ) -> None:
        return None

    monkeypatch.setattr(repository, "set_product_status", fake_set_product_status)


async def test_a_devops_install_failure_raises_devopserror_not_heal(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.sandbox = _ScriptedSandbox(
        results=[SandboxResult(exit_code=127, stdout="", stderr="sh: pip: not found")]
    )
    heal_called = False

    async def fake_heal(*args: object, **kwargs: object) -> str:
        nonlocal heal_called
        heal_called = True
        return "should not be reached"

    monkeypatch.setattr(dev_graph, "_heal", fake_heal)

    with pytest.raises(DevOpsError):
        await dev_graph._verify_and_ship(
            ctx,
            None,  # type: ignore[arg-type]
            product_id="prod-1",
            product_name="Test Product",
            workspace="product-1",
            test_command="pytest -q",
            idea_id="idea-1",
        )

    assert heal_called is False


async def test_the_install_step_runs_with_network_enabled(ctx: FactoryContext) -> None:
    """The historical bug this closes: dependency installation cannot succeed
    against the sandbox's default `--network none`."""
    sandbox = _ScriptedSandbox(
        results=[SandboxResult(exit_code=127, stdout="", stderr="sh: pip: not found")]
    )
    ctx.sandbox = sandbox

    with pytest.raises(DevOpsError):
        await dev_graph._verify_and_ship(
            ctx,
            None,  # type: ignore[arg-type]
            product_id="prod-1",
            product_name="Test Product",
            workspace="product-1",
            test_command="pytest -q",
            idea_id="idea-1",
        )

    assert sandbox.calls[0]["network"] is True


async def test_a_devops_test_run_failure_also_raises_devopserror(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.sandbox = _ScriptedSandbox(
        results=[
            SandboxResult(exit_code=0, stdout="", stderr=""),  # install succeeds
            SandboxResult(exit_code=126, stdout="", stderr="permission denied"),  # test run
        ]
    )
    heal_called = False

    async def fake_heal(*args: object, **kwargs: object) -> str:
        nonlocal heal_called
        heal_called = True
        return "should not be reached"

    monkeypatch.setattr(dev_graph, "_heal", fake_heal)

    with pytest.raises(DevOpsError):
        await dev_graph._verify_and_ship(
            ctx,
            None,  # type: ignore[arg-type]
            product_id="prod-1",
            product_name="Test Product",
            workspace="product-1",
            test_command="pytest -q",
            idea_id="idea-1",
        )

    assert heal_called is False


async def test_a_genuine_install_failure_still_heals_as_before(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad `requirements.txt` entry (e.g. a hallucinated package name) is a logic
    bug, not an infra problem, and must still reach `_heal()`."""
    ctx.sandbox = _ScriptedSandbox(
        results=[
            SandboxResult(
                exit_code=1,
                stdout="",
                stderr="ERROR: No matching distribution found for not-a-real-package==9.9.9",
            )
        ]
    )
    healed_with: dict[str, object] = {}

    async def fake_heal(
        ctx: FactoryContext,
        session: object,
        product_id: str,
        healing: str,
        idea_id: str,
        *,
        category: str,
    ) -> str:
        healed_with["category"] = category
        return "healed"

    monkeypatch.setattr(dev_graph, "_heal", fake_heal)

    result = await dev_graph._verify_and_ship(
        ctx,
        None,  # type: ignore[arg-type]
        product_id="prod-1",
        product_name="Test Product",
        workspace="product-1",
        test_command="pytest -q",
        idea_id="idea-1",
    )

    assert result == "healed"
    assert healed_with["category"] == "dependency_failure"
