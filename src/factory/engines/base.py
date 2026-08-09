"""Engine interface and lazy lookup.

Engines are resolved on demand so that a broken or unconfigured engine cannot stop
the Supervisor from importing and running the others.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from factory.settings import EngineName

if TYPE_CHECKING:  # pragma: no cover
    from factory.state.orm import Task
    from factory.supervisor.context import FactoryContext

EngineRunner = Callable[["FactoryContext", AsyncSession, "Task"], Awaitable[str]]

_MODULES: dict[EngineName, str] = {
    EngineName.DISCOVERY: "factory.engines.discovery.graph",
    EngineName.DEVELOPMENT: "factory.engines.development.graph",
    EngineName.MARKETING: "factory.engines.marketing.graph",
    EngineName.FINANCE: "factory.engines.finance.graph",
    EngineName.COMPLIANCE: "factory.engines.compliance.graph",
    EngineName.SUPERVISOR: "factory.engines.maintenance",
}


def get_runner(engine: EngineName) -> EngineRunner:
    """Import the engine's ``run`` coroutine on first use."""
    import importlib

    module_path = _MODULES[engine]
    module = importlib.import_module(module_path)
    runner = getattr(module, "run", None)
    if runner is None:  # pragma: no cover - guarded by tests over _MODULES
        raise AttributeError(f"{module_path} does not define run()")
    return runner  # type: ignore[no-any-return]
