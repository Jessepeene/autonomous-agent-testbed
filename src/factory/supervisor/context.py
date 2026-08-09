"""Shared runtime context.

Built once at boot and passed to every node, so an engine never reaches for a global
and tests can substitute fakes wholesale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from factory.hitl.interrupts import Notifier
from factory.llm.client import LLMClient
from factory.sandbox.runner import SandboxRunner
from factory.settings import EngineName, Settings
from factory.supervisor.routing import RoutingMemo


@dataclass(slots=True)
class FactoryContext:
    settings: Settings
    llm: LLMClient
    notifier: Notifier
    sandbox: SandboxRunner
    enabled_engines: set[EngineName]
    docker_available: bool = False
    worker_id: str = "supervisor-0"
    disabled_reasons: dict[EngineName, str] = field(default_factory=dict)
    #: Per-worker deterministic-routing state. Not durable — see `RoutingMemo`.
    routing: RoutingMemo = field(default_factory=RoutingMemo)

    def is_enabled(self, engine: EngineName) -> bool:
        return engine in self.enabled_engines

    @property
    def dry_run(self) -> bool:
        return self.settings.factory_dry_run
