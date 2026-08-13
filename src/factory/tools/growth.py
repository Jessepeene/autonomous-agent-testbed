"""Growth-role tools: GROWTH_ANALYST's read/act surface and CHANNEL_SCOUT's proposal
tool.

Two categories, deliberately split by blast radius rather than bundled into one:

``growth_read`` is read-only and shared by both roles -- a product-growth digest and
a staleness-ordered product list, so either role can self-select a product when
spawned without one named in its objective (the Orchestrator's own SPAWN_AGENT
choice, not the periodic backstop, which already names one).

``growth_actions`` is GROWTH_ANALYST-only and mutating: starting a live AB test or
requesting a growth-motivated SEO pass. Kept out of ``growth_read`` specifically so
CHANNEL_SCOUT -- which shares the read tools -- can never reach them.

``channel_scout`` is CHANNEL_SCOUT-only: reading past proposals for one product, and
proposing a new one. Proposing raises the mandatory human-approval interrupt in the
same call -- unlike ``campaign.ensure_approval``, there is no retried task polling
this, so the tool itself is the one and only place the interrupt gets raised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from factory.logging import get_logger
from factory.settings import EngineName, get_settings
from factory.state.schemas import InterruptReason, ProductStatus, TaskKind
from factory.tools.registry import REGISTRY

log = get_logger(__name__)


@REGISTRY.tool(
    name="read_product_growth",
    description=(
        "Read one product's traffic and conversion data: page views over the last "
        "7 and 30 days, total validation signals, and its currently running AB test "
        "(if any) with per-variant views and signals. Call this before deciding "
        "whether a product needs an AB test or an SEO pass."
    ),
    input_schema={
        "type": "object",
        "properties": {"product_id": {"type": "string"}},
        "required": ["product_id"],
        "additionalProperties": False,
    },
    category="growth_read",
)
async def read_product_growth(product_id: str) -> dict[str, Any]:
    from factory.state import repository
    from factory.state.db import session_scope

    settings = get_settings()
    async with session_scope(settings) as session:
        product = await repository.get_product(session, product_id)
        if product is None:
            return {"error": f"no product with id {product_id!r}"}

        now = datetime.now(UTC)
        views_7d = await repository.tally_page_views(
            session, product_id, since=now - timedelta(days=7)
        )
        views_30d = await repository.tally_page_views(
            session, product_id, since=now - timedelta(days=30)
        )
        signals = await repository.tally_validation_signals(session, product_id)

        running = await repository.get_running_ab_test(session, product_id)
        ab_test: dict[str, Any] | None = None
        if running is not None:
            outcomes = await repository.tally_ab_test_outcomes(
                session, product_id, since=running.started_at
            )
            a_views, a_signals = outcomes["a"]
            b_views, b_signals = outcomes["b"]
            ab_test = {
                "variant_a_brief": running.variant_a_brief,
                "variant_b_brief": running.variant_b_brief,
                "started_at": running.started_at.isoformat(),
                "variant_a": {"views": a_views, "signals": a_signals},
                "variant_b": {"views": b_views, "signals": b_signals},
            }

        return {
            "product_id": product_id,
            "name": product.name,
            "status": product.status,
            "deploy_url": product.deploy_url,
            "created_at": product.created_at.isoformat() if product.created_at else None,
            "views_last_7_days": views_7d,
            "views_last_30_days": views_30d,
            "validation_signals_total": signals,
            "running_ab_test": ab_test,
            "last_growth_review_at": (
                product.last_growth_review_at.isoformat() if product.last_growth_review_at else None
            ),
        }


@REGISTRY.tool(
    name="list_products_needing_growth_review",
    description=(
        "List live products that have gone the longest without a growth review "
        "(never-reviewed first). Call this only if you were not given a specific "
        "product to review."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximum products to return (1-20)."}
        },
        "required": [],
        "additionalProperties": False,
    },
    category="growth_read",
)
async def list_products_needing_growth_review(limit: int = 10) -> dict[str, Any]:
    from factory.state import repository
    from factory.state.db import session_scope

    settings = get_settings()
    cutoff = timedelta(days=settings.growth_review_interval_days)
    async with session_scope(settings) as session:
        products = await repository.products_needing_growth_review(
            session, older_than=cutoff, limit=max(1, min(limit, 20))
        )
        return {
            "products": [
                {
                    "product_id": p.id,
                    "name": p.name,
                    "deploy_url": p.deploy_url,
                    "last_growth_review_at": (
                        p.last_growth_review_at.isoformat() if p.last_growth_review_at else None
                    ),
                }
                for p in products
            ]
        }


@REGISTRY.tool(
    name="start_ab_test_action",
    description=(
        "Start a live, concurrent AB test on a product's landing/marketing page. "
        "Give two genuinely different, complete pitch briefs -- each is rendered as "
        "its own full variant, not a diff against the other. Fails harmlessly if a "
        "test is already running for this product."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "product_id": {"type": "string"},
            "variant_a_brief": {"type": "string", "minLength": 10, "maxLength": 2000},
            "variant_b_brief": {"type": "string", "minLength": 10, "maxLength": 2000},
        },
        "required": ["product_id", "variant_a_brief", "variant_b_brief"],
        "additionalProperties": False,
    },
    category="growth_actions",
)
async def start_ab_test_action(
    product_id: str, variant_a_brief: str, variant_b_brief: str
) -> dict[str, Any]:
    from factory.engines.development import graph as dev_graph
    from factory.state import repository
    from factory.state.db import session_scope

    settings = get_settings()
    async with session_scope(settings) as session:
        product = await repository.get_product(session, product_id)
        if product is None:
            return {"error": f"no product with id {product_id!r}"}
        ab_test_id = await dev_graph.start_ab_test(
            session,
            product_id=product_id,
            idea_id=product.idea_id,
            variant_a_brief=variant_a_brief,
            variant_b_brief=variant_b_brief,
        )
        if ab_test_id is None:
            return {"started": False, "reason": "an AB test is already running for this product"}
        return {"started": True, "ab_test_id": ab_test_id}


@REGISTRY.tool(
    name="request_seo_pass_action",
    description=(
        "Request a growth-motivated SEO/discoverability improvement pass on a LIVE "
        "product. Name the specific gap you found (e.g. 'the pricing page has almost "
        "no body copy, nothing for search engines to index') -- a vague 'improve "
        "SEO' cannot be acted on by the engineer who receives it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "product_id": {"type": "string"},
            "focus": {"type": "string", "minLength": 10, "maxLength": 2000},
        },
        "required": ["product_id", "focus"],
        "additionalProperties": False,
    },
    category="growth_actions",
)
async def request_seo_pass_action(product_id: str, focus: str) -> dict[str, Any]:
    from factory.state import repository
    from factory.state.db import session_scope
    from factory.state.orm import new_id

    settings = get_settings()
    async with session_scope(settings) as session:
        product = await repository.get_product(session, product_id)
        if product is None:
            return {"error": f"no product with id {product_id!r}"}
        if product.status != ProductStatus.LIVE.value:
            return {"error": "a growth SEO pass only applies to a LIVE product"}
        task_id = await repository.enqueue_task(
            session,
            kind=TaskKind.PATCH_PRODUCT,
            engine=EngineName.DEVELOPMENT,
            idempotency_key=f"patch:{product_id}:growth-seo:{new_id()}",
            payload={
                "product_id": product_id,
                "idea_id": product.idea_id,
                "healing": focus[:2000],
                # Routes to SEO_OPTIMIZER, not WORKSPACE_SURGEON -- see
                # development/graph.py's SURGICAL_CATEGORIES and _patch_surgically.
                "category": "growth_seo",
            },
            product_id=product_id,
            priority=60,
        )
        return {"requested": task_id is not None, "task_id": task_id}


@REGISTRY.tool(
    name="recent_channel_experiments",
    description=(
        "List this product's past and pending channel proposals (proposed, "
        "approved, rejected, or concluded with an outcome). Call this before "
        "proposing anything new, so you never re-propose the same channel."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "product_id": {"type": "string"},
            "limit": {"type": "integer", "description": "Maximum results (1-20)."},
        },
        "required": ["product_id"],
        "additionalProperties": False,
    },
    category="channel_scout",
)
async def recent_channel_experiments(product_id: str, limit: int = 10) -> dict[str, Any]:
    from factory.state import repository
    from factory.state.db import session_scope

    settings = get_settings()
    async with session_scope(settings) as session:
        rows = await repository.recent_channel_experiments(
            session, product_id, limit=max(1, min(limit, 20))
        )
        return {
            "experiments": [
                {
                    "channel": r.channel,
                    "url": r.url,
                    "status": r.status,
                    "outcome": r.outcome,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]
        }


@REGISTRY.tool(
    name="propose_channel",
    description=(
        "Propose a specific organic growth channel (a specific subreddit, forum "
        "thread, or community -- not a generic platform name) with a real draft "
        "post. Raises a human-approval request; nothing is posted automatically."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "product_id": {"type": "string"},
            "channel": {
                "type": "string",
                "maxLength": 40,
                "description": "Short channel label, e.g. 'reddit' or 'forum'.",
            },
            "url": {
                "type": "string",
                "maxLength": 500,
                "description": "The exact community/thread URL, e.g. a subreddit.",
            },
            "rationale": {"type": "string", "minLength": 10, "maxLength": 2000},
            "draft_content": {"type": "string", "minLength": 10, "maxLength": 6000},
        },
        "required": ["product_id", "channel", "url", "rationale", "draft_content"],
        "additionalProperties": False,
    },
    category="channel_scout",
)
async def propose_channel(
    product_id: str, channel: str, url: str, rationale: str, draft_content: str
) -> dict[str, Any]:
    from factory.hitl import interrupts
    from factory.state import repository
    from factory.state.db import session_scope

    settings = get_settings()
    async with session_scope(settings) as session:
        product = await repository.get_product(session, product_id)
        if product is None:
            return {"error": f"no product with id {product_id!r}"}

        experiment_id = await repository.create_channel_experiment(
            session,
            product_id=product_id,
            channel=channel,
            url=url,
            rationale=rationale,
            draft_content=draft_content,
        )
        # experiment_id[:8], not product_id or a fixed string, keeps this subject
        # unique per proposal -- open_interrupt() dedups on (reason, subject,
        # status), and two different proposals for the same product colliding on
        # subject text would silently collapse the second into the first (see
        # campaign.ensure_approval's identical reasoning for task_id[:8]).
        subject = f"Channel proposal ({experiment_id[:8]}): {channel} for {product.name[:80]}"
        detail = (
            f"Product: {product.name} ({product_id})\n"
            f"Channel: {channel}\n"
            f"URL: {url}\n\n"
            f"Rationale:\n{rationale}\n\n"
            f"Draft content -- post this yourself if you approve, the factory never "
            f"posts to external platforms automatically:\n\n{draft_content}"
        )
        # Deliberately no task_id/product_id passed to interrupts.request: both
        # route a reply through special-case handling in
        # hitl/interrupts.py::collect_replies (task retry / _act_on_product_reply,
        # which means "retire this product" on REJECT) built for entirely
        # different escalation shapes. This experiment's own interrupt_id is the
        # only link back -- see channel.sync_decisions.
        interrupt_id = await interrupts.request(
            session,
            reason=InterruptReason.CHANNEL_APPROVAL,
            subject=subject,
            detail=detail,
            headline="\U0001f4e3 Approval needed — new growth channel",
            reply_hint=(
                "Reply APPROVE to authorise (you post the draft yourself), or REJECT to decline."
            ),
        )
        if interrupt_id is not None:
            await repository.set_channel_experiment_interrupt(session, experiment_id, interrupt_id)
        return {
            "proposed": True,
            "experiment_id": experiment_id,
            "awaiting_human_approval": interrupt_id is not None,
        }
