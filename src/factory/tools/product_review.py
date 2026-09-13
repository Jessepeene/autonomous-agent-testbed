"""Turning a product review's verdict into real work.

Deliberately **not** an agent tool. It shipped as one (`report_product_issue`,
in AUDITOR's own tool set) and that design failed twice in production for the
same reason: both live runs spent their whole step budget investigating and
never chose to call it, so a genuine review was performed and then discarded.
Asking an agent on an open-ended task to remember to stop and file a report is
not a reliable control.

So the split is now the one `learning/maintenance.py` already uses: the agent
investigates and writes prose; a separate, always-run structured call extracts a
validated verdict from that prose; and this function acts on the verdict. The
agent no longer decides *whether* its findings get recorded -- only what they
are.

Lives under `tools/` because it is still the review's action surface and its
guards (product still LIVE, dry-run, one patch per bucket) are the same ones a
tool would need.
"""

from __future__ import annotations

import time

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.settings import EngineName
from factory.state import repository
from factory.state.schemas import Contract, ProductStatus, TaskKind

log = get_logger(__name__)

#: One review-commissioned patch per product per bucket. A reviewer that finds
#: three defects should describe all three in one patch request, not queue three
#: competing rewrites of the same workspace -- and a review re-run tomorrow on a
#: product whose patch has not shipped yet must not stack a second one on top.
PATCH_BUCKET_SECONDS = 86_400


class ReviewVerdict(Contract):
    """The structured read of what the reviewer concluded.

    Extracted from the agent's own final message rather than self-reported
    through a tool call, so a review that ran out of steps mid-investigation
    still yields whatever it had established by then.
    """

    #: Deliberately the first field: everything else is only meaningful once this
    #: is true, and a reviewer that found nothing must have a cheap way to say so.
    defect_found: bool
    #: Generous rather than tight, and the target lengths live in the prompt
    #: instead (`_REVIEW_VERDICT_SYSTEM`).
    #:
    #: These were 200/2000, and the prompt -- which asks for "what was done, what
    #: happened, what should have happened", for "an engineer who cannot see the
    #: page" -- never mentioned either number. The first live POC review wrote a
    #: perfectly good writeup, overran 200 on `summary`, had its repair attempt
    #: overrun 2000 on `details`, and the whole review was discarded: a real defect
    #: found on a real page and thrown away over a limit nobody told the model
    #: about. That is the same outcome as the step-limit loss this extraction step
    #: was built to prevent, arriving by a different door.
    #:
    #: A cap still exists so genuinely absurd output fails rather than flowing into
    #: a task payload, and `queue_review_patch` truncates what it forwards anyway.
    summary: str = Field(default="", max_length=500)
    details: str = Field(default="", max_length=4000)


async def queue_review_patch(
    session: AsyncSession,
    *,
    product_id: str,
    verdict: ReviewVerdict,
    dry_run: bool,
) -> tuple[str | None, str]:
    """Commission a fix for a real finding. Returns ``(task_id, reason)``.

    ``task_id`` is None for every non-action path -- nothing found, dry run, the
    product is no longer LIVE, or a patch is already queued this bucket -- with
    ``reason`` explaining which, for the caller's log line.
    """
    if not verdict.defect_found:
        return None, "no defect found"
    if not verdict.summary.strip() or not verdict.details.strip():
        # A verdict claiming a defect with nothing concrete in it is not
        # actionable by the engineer who receives it, and would burn a real
        # repair cycle chasing prose. Treated as no finding.
        log.warning("product_review.empty_verdict", product_id=product_id)
        return None, "defect claimed but no specifics given"
    if dry_run:
        # AUDITOR is constructed directly (it needs an injected private registry),
        # so `spawner.can_spawn`'s dry-run refusal never runs for it. Enforce the
        # same rule here rather than silently commissioning work.
        return None, "FACTORY_DRY_RUN is set; finding recorded but no patch queued"

    product = await repository.get_product(session, product_id)
    if product is None:  # pragma: no cover - caller pins a real product
        return None, f"no product with id {product_id!r}"
    if product.status != ProductStatus.LIVE.value:
        # Re-derived here rather than trusted from the caller's earlier check: a
        # product can start a build between the review starting and this call, and
        # patching mid-build fights `product_lock`.
        return None, f"product is {product.status}, not live -- a patch would collide"

    bucket = int(time.time()) // PATCH_BUCKET_SECONDS
    healing = f"{verdict.summary.strip()}\n\n{verdict.details.strip()}"
    task_id = await repository.enqueue_task(
        session,
        kind=TaskKind.PATCH_PRODUCT,
        engine=EngineName.DEVELOPMENT,
        idempotency_key=f"patch:{product_id}:review:{bucket}",
        payload={
            "product_id": product_id,
            "idea_id": product.idea_id,
            "healing": healing[:2000],
            # `guided`, not a council/test category: there is no failing test here.
            # A reviewer used the running product and said what is wrong, which is
            # exactly the shape `repair.py::attempt_repair` already treats as
            # guidance rather than a failure to reproduce.
            "category": "guided",
        },
        product_id=product_id,
        priority=60,
    )
    if task_id is None:
        return None, "a review-requested patch is already queued for this product today"
    log.info("product_review.patch_requested", product_id=product_id, task_id=task_id)
    return task_id, "patch queued"
