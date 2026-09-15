"""DevOps-error classification and routing.

Rewriting generated code cannot fix a missing binary, a permission block, or an
unreachable package registry. These tests cover the classifier itself
(`is_devops_error`) and that `_verify_and_ship` actually raises `DevOpsError` — never
calls `_heal()` — for exactly that class of sandbox failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from factory.engines.development import bug_council
from factory.engines.development import deploy as deploy_module
from factory.engines.development import graph as dev_graph
from factory.sandbox.runner import DevOpsError, SandboxResult, is_devops_error
from factory.settings import Settings
from factory.state import repository
from factory.state.schemas import CouncilResult, ProductStatus
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


@pytest.mark.parametrize("exit_code", [126, 127, 139])
def test_conventional_infra_exit_codes_are_classified_as_devops(exit_code: int) -> None:
    result = SandboxResult(exit_code=exit_code, stdout="", stderr="")
    assert is_devops_error(result) is True


def test_exit_137_alone_is_not_classified_as_devops() -> None:
    """Regression test: both call sites check `result.timed_out` and return via a
    dedicated heal category *before* is_devops_error is ever consulted, so a 137
    that reaches this function was never our own timeout kill -- it is almost
    always the sandbox's own --memory cgroup OOM-killing a process inside the
    container, i.e. the generated app's own unbounded memory growth. That is an
    application bug (exactly what the security reviewer's compute-burn mandate
    hunts for), not infrastructure, and must be allowed to heal."""
    result = SandboxResult(exit_code=137, stdout="", stderr="")
    assert is_devops_error(result) is False


def test_a_normal_assertion_failure_is_not_classified_as_devops() -> None:
    result = SandboxResult(
        exit_code=1,
        stdout="FAILED tests/test_app.py::test_signup - AssertionError: assert 400 == 201",
        stderr="",
    )
    assert is_devops_error(result) is False


def test_an_assertion_about_permission_denied_text_is_not_classified_as_devops() -> None:
    """Regression test: a bare substring match on "permission denied" previously
    misrouted a real application bug -- a generated test's own assertion output
    quoting that exact phrase -- into the devops-escalation path instead of a
    heal cycle."""
    result = SandboxResult(
        exit_code=1,
        stdout=(
            "FAILED tests/test_auth.py::test_admin_only - AssertionError: "
            "assert detail == 'Permission denied'"
        ),
        stderr="",
    )
    assert is_devops_error(result) is False


def test_a_real_shell_permission_error_is_still_classified_as_devops() -> None:
    result = SandboxResult(
        exit_code=1,
        stdout="",
        stderr="mkdir: cannot create directory '/workspace/out': Permission denied",
    )
    assert is_devops_error(result) is True


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
        self.calls.append({"command": command, "network": network, "env": env})
        return self.results.pop(0)


@pytest.fixture(autouse=True)
def _workspace_exists(settings: Settings) -> None:
    """`product-1` is the workspace every test here verifies and ships.

    Created explicitly because `resolve_workspace` no longer brings a workspace into
    existence just by being asked about one -- in production `write_plan` has always
    created it before `_verify_and_ship` runs, and a name nobody created is now an
    error rather than a silently empty directory.
    """
    (settings.workspace_root / "product-1").mkdir(parents=True, exist_ok=True)


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

    @dataclass(slots=True)
    class _FakeIdeaRow:
        self_serve_price_usd: float = 49.0

    async def fake_get_idea(session: object, idea_id: str) -> _FakeIdeaRow:
        # Only reached for a real (non-validation) build that got past the
        # contract gate, right before deploy -- see _verify_and_ship's price
        # lookup.
        return _FakeIdeaRow()


    async def fake_live_requirements(
        session: object, subject_kind: str, subject_id: str
    ) -> None:
        # No stated standard, which is the pre-requirements world every one of
        # these tests was written against -- `decide_release` says so in its
        # rationale rather than inventing one.
        return None

    async def fake_open_issues_for(session: object, product_id: str) -> list[object]:
        return []

    async def fake_record_task_event(session: object, **kwargs: object) -> None:
        return None


    async def fake_get_product(session: object, product_id: str) -> None:
        # None is the pre-existing convention in the sibling fixture: a product the
        # stub cannot look up simply skips the checks that need the row.
        return None


    async def fake_get_build_kind(session: object, product_id: str) -> str:
        # The single fact `_verify_and_ship` reads to know what it is verifying.
        # These tests are all about a real MVP build reaching (or not reaching) the
        # council and deploy, so `mvp` is the kind under test.
        return "mvp"


    async def fake_product_progress(session: object, product_id: str) -> object:
        # The convergence thread, assembled from rows in production. Nothing here
        # exercises it, so a first-attempt shape (nothing to compare against) keeps
        # the release judgement on its normal path.
        from factory.state.repository import ProductProgress

        return ProductProgress(
            attempts=0,
            blocking_now=0,
            blocking_at_last_attempt=None,
            stuck=[],
            last_decision=None,
        )

    async def fake_close_resolved_issues(session: object, **kwargs: object) -> int:
        # The deterministic checks close what they no longer find, so their silence
        # can lower `blocking_now`. Nothing here exercises that; it just needs the
        # write path stubbed like every other repository call in this fixture.
        return 0

    async def fake_open_interrupt(session: object, **kwargs: object) -> str:
        # The pre-ship review cannot start a browser in a unit run, and a review
        # that could not run now says so out loud instead of shipping in silence --
        # which means this fixture needs the interrupt write path stubbed too.
        return "interrupt-1"

    monkeypatch.setattr(repository, "open_interrupt", fake_open_interrupt)
    monkeypatch.setattr(repository, "close_resolved_issues", fake_close_resolved_issues)
    monkeypatch.setattr(repository, "set_product_status", fake_set_product_status)
    monkeypatch.setattr(repository, "product_progress", fake_product_progress)
    monkeypatch.setattr(repository, "get_build_kind", fake_get_build_kind)
    monkeypatch.setattr(repository, "get_product", fake_get_product)
    monkeypatch.setattr(repository, "live_requirements", fake_live_requirements)
    monkeypatch.setattr(repository, "open_issues_for", fake_open_issues_for)
    monkeypatch.setattr(repository, "record_task_event", fake_record_task_event)
    monkeypatch.setattr(repository, "get_idea", fake_get_idea)


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


# --- install/test cross-container persistence -----------------------------------


async def test_install_and_test_are_separate_containers_that_do_not_share_state(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install step and the test run are two separate `docker run` invocations
    (kept separate deliberately, for distinct devops-error detail) -- nothing
    outside the bind-mounted /workspace survives between them. Installing into a
    workspace-relative --target and pointing PYTHONPATH/PATH there for the test run
    is what makes the install actually visible to it; a bare `pip install` would
    land in the install container's own (ephemeral) filesystem and be gone by the
    time the test container starts."""
    sandbox = _ScriptedSandbox(
        results=[
            SandboxResult(exit_code=0, stdout="", stderr=""),  # install
            SandboxResult(exit_code=1, stdout="1 failed, 4 passed", stderr=""),  # tests
        ]
    )
    ctx.sandbox = sandbox

    async def fake_heal(*args: object, **kwargs: object) -> str:
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
    install_call, test_call = sandbox.calls[0], sandbox.calls[1]
    assert "--target=/workspace/.deps" in str(install_call["command"])
    # PRODUCT_ID/STRIPE_API_KEY/PRODUCT_PRICE_CENTS: dummy values so a generated
    # test that exercises the request path doesn't crash on KeyError -- see
    # _verify_and_ship's own comment at this env= call.
    assert test_call["env"] == {
        "PYTHONPATH": "/workspace/.deps",
        "PRODUCT_ID": "test-product-id",
        "STRIPE_API_KEY": "sk_test_dummy",
        "PRODUCT_PRICE_CENTS": "4900",
    }
    assert "/workspace/.deps/bin" in str(test_call["command"])
    assert "pytest -q" in str(test_call["command"])


# --- Bug Council / deploy infra failures also route away from _heal() -----------


def _passing_sandbox() -> _ScriptedSandbox:
    return _ScriptedSandbox(
        results=[
            SandboxResult(exit_code=0, stdout="", stderr=""),  # install
            SandboxResult(exit_code=0, stdout="5 passed", stderr=""),  # tests
        ]
    )


async def test_council_unavailable_raises_devopserror_not_heal(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.sandbox = _passing_sandbox()

    async def fake_convene(*args: object, **kwargs: object) -> CouncilResult:
        raise bug_council.CouncilUnavailable("3 reviewer(s) could not complete: 503; 503; 503")

    heal_called = False

    async def fake_heal(*args: object, **kwargs: object) -> str:
        nonlocal heal_called
        heal_called = True
        return "should not be reached"

    monkeypatch.setattr(bug_council, "convene", fake_convene)
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


async def test_infra_deploy_failure_raises_devopserror_not_heal(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.sandbox = _passing_sandbox()

    async def fake_convene(*args: object, **kwargs: object) -> CouncilResult:
        return CouncilResult(approved=True, verdicts=[])

    async def fake_deploy(*args: object, **kwargs: object) -> deploy_module.DeployResult:
        return deploy_module.DeployResult(
            ok=False, url=None, detail="vercel returned 503", is_infra=True
        )

    heal_called = False

    async def fake_heal(*args: object, **kwargs: object) -> str:
        nonlocal heal_called
        heal_called = True
        return "should not be reached"

    monkeypatch.setattr(bug_council, "convene", fake_convene)
    monkeypatch.setattr(deploy_module, "deploy", fake_deploy)
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


async def test_non_infra_deploy_failure_still_heals_as_before(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely malformed deploy payload (not infra) must still reach _heal() --
    a different generation attempt might avoid it."""
    ctx.sandbox = _passing_sandbox()

    async def fake_convene(*args: object, **kwargs: object) -> CouncilResult:
        return CouncilResult(approved=True, verdicts=[])

    async def fake_deploy(*args: object, **kwargs: object) -> deploy_module.DeployResult:
        return deploy_module.DeployResult(
            ok=False, url=None, detail="workspace contained no deployable files", is_infra=False
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

    monkeypatch.setattr(bug_council, "convene", fake_convene)
    monkeypatch.setattr(deploy_module, "deploy", fake_deploy)
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
    assert healed_with["category"] == "deploy_failure"
