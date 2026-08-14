"""Turning subscription churn into evidence Discovery can actually use.

Deliberately deferred out of `finance/webhooks.py`'s request/response cycle:
that handler's only job, everywhere else in this module, is DB/ledger state --
no LLM calls, no memory writes, no agent spawns happen synchronously inside a
webhook handler anywhere in this codebase today. `memory_store.remember` itself
is cheap (lexical, not embedded -- see its own module docstring), so this
isn't about a timeout risk specifically; it's about keeping the webhook
handler in the same narrow lane every other handler in that file already
occupies. This module is what the periodic `review-churn` maintenance item
(`engines/maintenance.py`) calls instead, observing DB state a webhook already
wrote rather than acting inline.

Both churn kinds are written to memory, not just involuntary: a hardcoded
decision to discard involuntary churn as "noise" before Discovery ever sees it
would be exactly the kind of rigid rule this factory's own design principle
argues against (see `compliance/assessor.py`'s own reasoning for reading
structured facts as evidence, never an auto-reject) -- the content instead
honestly distinguishes the two, so the model does the weighing, not this code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName
from factory.agents.spawner import SpawnRequest, spawn
from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.settings import EngineName
from factory.state import repository
from factory.state.orm import Task
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: How far back `review_churn` looks for products to hand to GROWTH_ANALYST --
#: matches `FactorySnapshot.churning_products`' own window, so the maintenance
#: item and the Orchestrator's own sense of "who's churning" never disagree.
CHURN_REVIEW_WINDOW_DAYS = 30


async def write_churn_lessons(session: AsyncSession) -> int:
    """Writes one memory per churned subscription this hasn't already been done
    for, at `engine:discovery` scope. Returns how many were written.

    Skips (but still marks as processed) a churn with no attributable product
    -- there is no niche to name, and `memory_store.remember`'s own minimum
    length would reject a niche-less sentence as noise anyway.
    """
    written = 0
    for subscription in await repository.unremembered_churned_subscriptions(session):
        niche = (
            await repository.get_niche_for_product(session, subscription.product_id)
            if subscription.product_id
            else None
        )
        if niche is not None:
            if subscription.churn_kind == "involuntary":
                content = (
                    f"A customer in the niche '{niche}' churned involuntarily "
                    f"(payment failure, dunning did not recover it). This can mean "
                    f"a real payment problem, or it can mean a disengaged customer "
                    f"who never bothered to update a failed card -- weigh it "
                    f"alongside other signals for this niche, not as a standalone "
                    f"verdict."
                )
            else:
                content = (
                    f"A customer in the niche '{niche}' voluntarily canceled their "
                    f"subscription. This is a direct signal about product-market "
                    f"fit, pricing, or whether the problem recurs often enough to "
                    f"justify ongoing payment -- weigh it more heavily than an "
                    f"involuntary payment failure in the same niche."
                )
            await memory_store.remember(
                session,
                content=content,
                kind=memory_store.MemoryKind.FAILURE,
                scope=memory_store.scope_for(engine=EngineName.DISCOVERY),
                origin="finance:churn",
            )
            written += 1
        await repository.mark_churn_memory_written(session, subscription.id)
    return written


async def review_churn(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """The `review-churn` maintenance item: writes the deterministic lessons
    above, then spawns the existing GROWTH_ANALYST role once per currently
    churning product -- no new role, no new tool, the same agent that already
    reviews traffic/AB-test state, just with a churn-motivated reason to look.
    """
    memory_count = await write_churn_lessons(session)

    since = datetime.now(UTC) - timedelta(days=CHURN_REVIEW_WINDOW_DAYS)
    product_ids = await repository.churning_products(session, since=since)
    if not product_ids:
        return f"finance/churn: {memory_count} lesson(s) written, no products currently churning"

    reviewed = 0
    for product_id in product_ids:
        product = await repository.get_product(session, product_id)
        if product is None:
            continue
        objective = (
            f"Product {product_id} ({product.name}) has had a customer churn in the last "
            f"{CHURN_REVIEW_WINDOW_DAYS} days. Review its growth state and decide whether "
            f"the churn reflects an acquisition-quality or positioning problem you can act "
            f"on (a distribution/SEO change to attract a better-fit customer, or an AB test "
            f"on the pitch), or whether this looks like normal attrition that needs no action."
        )
        await spawn(
            ctx,
            session,
            SpawnRequest(
                role=RoleName.GROWTH_ANALYST,
                objective=objective,
                engine=EngineName.FINANCE,
                product_id=product_id,
                task_id=task.id,
            ),
        )
        reviewed += 1

    return (
        f"finance/churn: {memory_count} lesson(s) written, "
        f"GROWTH_ANALYST reviewed {reviewed}/{len(product_ids)} churning product(s)"
    )
