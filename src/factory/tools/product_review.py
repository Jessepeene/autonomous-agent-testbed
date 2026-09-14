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

from factory import issues
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
    #: No length cap at all, and the target lengths live in the prompt instead
    #: (`_REVIEW_VERDICT_SYSTEM`). See `Contract`'s docstring: every max_length
    #: chosen for a prose field here has been wrong, they bound nothing the database
    #: needs, and `max_tokens` is the real ceiling.
    #:
    #: These were 200/2000. The prompt -- which asks for "what was done, what
    #: happened, what should have happened", for "an engineer who cannot see the
    #: page" -- never mentioned either number. The first live POC review wrote a
    #: perfectly good writeup, overran 200 on `summary`, had its repair attempt
    #: overrun 2000 on `details`, and the whole review was discarded: a real defect
    #: on a real page thrown away over a limit nobody told the model about. Raising
    #: them to 500/4000 only moved the cliff; `queue_review_patch` truncates what it
    #: forwards anyway, so the cap was never protecting anything downstream.
    summary: str = Field(default="")
    details: str = Field(default="")
    #: Where the defect lives -- an endpoint, a selector, a file:line, a page area.
    #: This is what gives a finding an identity that survives rewording, so the
    #: same defect found next week matches the existing issue instead of filing a
    #: fifth copy of it (see `factory/issues.py`). Best-effort: a reviewer that
    #: cannot name one still gets an issue, matched on wording instead.
    location: str = Field(default="")
    kind: str = Field(default="bug")
    severity: str = Field(default="medium")


async def queue_review_patch(
    session: AsyncSession,
    *,
    product_id: str,
    verdict: ReviewVerdict,
    dry_run: bool,
    found_by: str = "auditor",
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

    # File the finding *before* commissioning work, and file it whether or not the
    # patch is queued. The issue is the durable record; the task is one attempt at
    # it. Getting this order wrong is how a defect blocked by the once-a-day patch
    # bucket used to vanish entirely.
    issue, is_new = await repository.record_issue(
        session,
        product_id=product_id,
        kind=verdict.kind if issues.is_actionable(verdict.kind) else "bug",
        severity=verdict.severity if verdict.severity in issues.SEVERITIES else "medium",
        title=verdict.summary.strip(),
        location=verdict.location.strip(),
        actual=verdict.details.strip(),
        found_by=found_by,
    )
    if not is_new:
        log.info(
            "product_review.known_issue",
            issue_id=issue.id,
            times_seen=issue.times_seen,
            first_seen=issue.first_seen_at.isoformat(),
        )

    bucket = int(time.time()) // PATCH_BUCKET_SECONDS
    # Where it was seen leads the brief, because that is what the engineer receiving
    # it cannot otherwise work out. A repair agent handed "checkout returns 422" and
    # shown source that already contains a fix has a genuine contradiction on its
    # hands: it cannot tell whether the report is stale or the fix is unshipped.
    # Observed live -- one spent six of sixteen steps unpacking a .pyc header trying
    # to infer which build was real, and ran out of budget before acting.
    healing = (
        f"Observed on the running deployment at {product.deploy_url}, by an "
        "automated QA review that used the product as a visitor would. Not "
        "reproduced from this workspace, and not from a failing test.\n\n"
        f"{verdict.summary.strip()}\n\n{verdict.details.strip()}"
    )
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
        return None, f"issue {issue.id} recorded; a patch is already queued for it today"

    # The delegation step, and the link that makes closure possible: this issue is
    # now with this role, being worked by this task. Without it, "done" on the task
    # could never mean anything about the defect.
    await repository.assign_issue_fix(
        session, issue.id, task_id=task_id, assigned_to="repair_engineer"
    )
    log.info(
        "product_review.patch_requested", product_id=product_id, task_id=task_id, issue=issue.id
    )
    return task_id, f"issue {issue.id} queued for repair"
