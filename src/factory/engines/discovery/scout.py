"""DISCOVERY_SCOUT: an agentic complement to the fixed HN/GitHub/Reddit/Product
Hunt sources.

Those sources (`sources/`) return `Signal` objects directly from a plain HTTP
call, in-process, no model involved -- and every one of them searches the same
fixed phrase list every cycle (`sources/base.py::COMPLAINT_QUERIES`,
`sources/github.py::_QUERIES`), which is why repeated cycles inside a short
window return near-identical results. DISCOVERY_SCOUT is a genuine agent, free
to search wherever it judges useful and varying its own queries between runs --
the same shape as CHANNEL_SCOUT's free web exploration (`agents/roles.py`),
applied to problem-seed discovery instead of growth channels.

Its findings cross back into this cycle through `scout_signals`, a small,
strictly ephemeral Postgres table (`state/orm.py::ScoutSignal`) this same call
drains immediately after the agent finishes -- not an in-process return value,
because a tool call in this codebase has no channel back to its caller except
the database (see `tools/discovery.py::submit_signal`).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.engines.discovery.sources.base import Signal
from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Task
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)


async def scout_signals(ctx: FactoryContext, session: AsyncSession, task: Task) -> list[Signal]:
    """Spawn DISCOVERY_SCOUT for one Discovery cycle and return whatever it found.

    Never raises: a refused or failed spawn just means zero additional signals,
    exactly like one fixed source failing in `graph.py::gather_signals` does not
    lose the others.
    """
    if not ctx.settings.discovery_scout_enabled:
        return []

    objective = (
        "Find real evidence of an unmet need that the fixed HN/GitHub/Reddit/"
        "Product Hunt sources would not already surface this cycle, and call "
        "submit_signal for each real finding.\n"
        f"Your task_id for every submit_signal call is exactly: {task.id}"
    )
    result = await spawn(
        ctx,
        session,
        SpawnRequest(
            role=RoleName.DISCOVERY_SCOUT,
            objective=objective,
            engine=EngineName.DISCOVERY,
            task_id=task.id,
        ),
    )
    rows = await repository.drain_scout_signals(session, task.id)
    log.info(
        "discovery.scouted",
        steps=result.steps,
        stopped=result.stopped_because,
        found=len(rows),
    )
    return [
        Signal(
            title=row.title,
            body=row.body,
            url=row.url,
            source=f"scout:{row.source}",
            engagement=row.engagement,
        )
        for row in rows
    ]
