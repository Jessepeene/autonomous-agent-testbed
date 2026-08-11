"""Turning a resolved channel-approval interrupt into a decision.

Mirrors `campaign.py`'s `sync_decisions` exactly, and for the same reason: the
interrupt reply is free text collected by `hitl/interrupts.py::collect_replies`
into `Interrupt.response`, and something has to fail-closed-parse it into the
`ChannelExperiment` row's real status. Run once per tick from
`Scheduler.housekeeping`, right after `campaign.sync_decisions`.

Unlike a campaign approval, there is no retried task waiting on this decision --
`tools/growth.py::propose_channel` raises the interrupt directly, in the same call
that creates the row. This module only ever turns a "resolved" interrupt into the
row's terminal `approved`/`rejected` status; a human then posts the draft
themselves (see the interrupt's own text), and the measurement sweep
(`marketing/graph.py::_measure_channel_experiment`) picks up from there.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.state import repository
from factory.state.orm import Interrupt

log = get_logger(__name__)


def _parse_decision(response: str) -> str:
    """Fail-closed, same contract as `campaign._parse_decision`: only an explicit,
    unambiguous "approve" authorises anything. Anything else is a rejection."""
    normalized = response.strip().lower()
    return "approved" if normalized.startswith("approve") else "rejected"


async def sync_decisions(session: AsyncSession) -> int:
    synced = 0
    for experiment in await repository.pending_channel_experiments(session):
        assert experiment.interrupt_id is not None  # guaranteed by the query filter
        interrupt = await session.get(Interrupt, experiment.interrupt_id)
        if interrupt is None or interrupt.status != "resolved":
            continue
        decision = _parse_decision(interrupt.response or "")
        await repository.decide_channel_experiment(session, experiment.id, decision)
        log.info(
            "channel.decided",
            product_id=experiment.product_id,
            experiment_id=experiment.id,
            channel=experiment.channel,
            decision=decision,
        )
        synced += 1
    return synced
