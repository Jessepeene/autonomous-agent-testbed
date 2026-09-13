"""Internal MCP tool registry.

Tools are runtime-discoverable descriptors, not static imports. When the
Orchestrator spins up an ephemeral sub-agent it asks for the categories that agent
needs — ``get_mcp_tools(category=["browser", "git"])`` — and attaches only those
schemas to the model payload.

Two filters run before anything is attached, because a tool the agent cannot
actually use is worse than an absent one: it burns tokens describing a capability
and then fails mid-task.

* ``required_credential`` missing  → not offered.
* ``requires_sandbox`` with no Docker daemon → not offered.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from factory.logging import get_logger
from factory.settings import Settings, get_settings

log = get_logger(__name__)

ToolHandler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """An MCP-shaped tool definition plus the preconditions for offering it."""

    name: str
    description: str
    input_schema: dict[str, Any]
    category: str
    handler: ToolHandler
    requires_sandbox: bool = False
    required_credential: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }

    def to_anthropic(self) -> dict[str, Any]:
        schema = dict(self.input_schema)
        schema.setdefault("additionalProperties", False)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
            "strict": True,
        }

    def to_gemini(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.input_schema,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDescriptor] = {}

    @classmethod
    def extending(cls, base: ToolRegistry) -> ToolRegistry:
        """A private registry that starts with everything `base` has.

        `tools/browser.py` set the precedent for a per-run private registry, but it
        *replaces* the global one — which is right for AUDITOR, whose only category
        is `browser`. A tool that must be bound to the run it serves while the role
        keeps all its normal tools needs the additive form instead: copy the
        descriptors, then register the bound ones on top.

        The copy is shallow and the descriptors are immutable, so a per-run
        registry cannot leak a bound handler back into the process-wide one.
        """
        extended = cls()
        extended._tools = dict(base._tools)
        return extended

    def register(self, descriptor: ToolDescriptor) -> ToolDescriptor:
        if descriptor.name in self._tools:
            raise ValueError(f"tool {descriptor.name!r} is already registered")
        self._tools[descriptor.name] = descriptor
        return descriptor

    def tool(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        category: str,
        requires_sandbox: bool = False,
        required_credential: str | None = None,
        tags: Sequence[str] = (),
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorator(handler: ToolHandler) -> ToolHandler:
            self.register(
                ToolDescriptor(
                    name=name,
                    description=description,
                    input_schema=input_schema,
                    category=category,
                    handler=handler,
                    requires_sandbox=requires_sandbox,
                    required_credential=required_credential,
                    tags=tuple(tags),
                )
            )
            return handler

        return decorator

    def get(self, name: str) -> ToolDescriptor | None:
        return self._tools.get(name)

    def all(self) -> list[ToolDescriptor]:
        return list(self._tools.values())

    def categories(self) -> set[str]:
        return {t.category for t in self._tools.values()}

    def usable(
        self,
        descriptor: ToolDescriptor,
        *,
        settings: Settings,
        docker_available: bool,
    ) -> bool:
        if descriptor.requires_sandbox and not docker_available:
            return False
        if descriptor.required_credential is not None:
            if settings.secret(descriptor.required_credential) is None:
                return False
        return True

    def get_mcp_tools(
        self,
        *,
        category: Iterable[str] | None = None,
        settings: Settings | None = None,
        docker_available: bool = True,
    ) -> list[ToolDescriptor]:
        """Descriptors an ephemeral agent in these categories can actually run."""
        settings = settings or get_settings()
        wanted = set(category) if category is not None else None
        selected: list[ToolDescriptor] = []
        for descriptor in self._tools.values():
            if wanted is not None and descriptor.category not in wanted:
                continue
            if not self.usable(descriptor, settings=settings, docker_available=docker_available):
                log.debug(
                    "tools.filtered",
                    tool=descriptor.name,
                    reason=(
                        "sandbox_unavailable"
                        if descriptor.requires_sandbox and not docker_available
                        else "missing_credential"
                    ),
                )
                continue
            selected.append(descriptor)
        return sorted(selected, key=lambda d: d.name)


#: Process-wide registry. Tool modules populate it on import.
REGISTRY = ToolRegistry()


def anthropic_payload(descriptors: Sequence[ToolDescriptor]) -> list[dict[str, Any]]:
    return [d.to_anthropic() for d in descriptors]


def gemini_payload(descriptors: Sequence[ToolDescriptor]) -> list[dict[str, Any]]:
    return [d.to_gemini() for d in descriptors]


def mcp_payload(descriptors: Sequence[ToolDescriptor]) -> list[dict[str, Any]]:
    return [d.to_mcp() for d in descriptors]
