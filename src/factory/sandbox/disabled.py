"""The fail-closed sandbox.

Selected whenever no Docker daemon is reachable. Every call raises, which is the
point: the Development Engine must stall rather than quietly execute model-written
code on the host that runs the factory's own credentials.
"""

from __future__ import annotations

from factory.sandbox.runner import SandboxResult, SandboxUnavailable


class DisabledSandbox:
    @property
    def available(self) -> bool:
        return False

    async def execute(
        self,
        command: str,
        *,
        workspace: str,
        timeout_seconds: int | None = None,
        network: bool = False,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        raise SandboxUnavailable(
            "No Docker daemon is reachable, so generated code cannot be executed. "
            "The Development Engine stays disabled until Docker is available; the "
            "factory does not run untrusted code on the host."
        )
