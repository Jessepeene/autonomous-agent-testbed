"""The work the Design manager commissions: write guidelines, and look at a page.

Two jobs, deliberately separate from the manager that decides they are worth
doing. The manager judges *what deserves attention*; these do the thing.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.engines.development.requirements import HOUSE_STYLE, render_design_for_prompt
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.orm import Task
from factory.state.schemas import DesignGuidelines, VisualReview
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

GUIDELINES_SYSTEM = """\
You decide how one product should look, before anyone builds it.

Write for *this* product and the people who will see it. A tool for CI engineers \
and a tool for bookkeepers should not arrive at the same page, and if your \
guidelines would fit either, you have written a house style rather than a \
direction.

Ground the look in the buyer's own world -- what they already stare at all day. \
Terminal output, build logs and pipeline diagrams for infrastructure people; \
ledgers and reconciliation for finance; whatever is true for this audience. \
Specific and slightly unexpected beats tasteful and anonymous, because anonymous \
is precisely what a generated page looks like.

Everything you write must be **checkable by looking at the built page**. That is \
the difference between a guideline and an opinion: "the page loads a display face \
that is not a system font" can be failed, "professional and modern" cannot, and a \
reviewer who cannot fail a guideline honestly will pass everything.

The house style below is the company's baseline. Inherit what suits this product, \
and say plainly where you are departing from it and why -- a departure you can \
justify is the point of doing this per product.\
"""

REVIEW_SYSTEM = """\
You are looking at a screenshot of a page that is live in front of real people, \
and judging it against the guidelines it was built to.

You can see it. Judge what you can see, not what the markup implies.

Separate two kinds of finding, because they are treated differently and conflating \
them is how a design review stops products shipping:

**broken** -- something a viewer cannot use or read. Text that fails contrast, an \
element overlapping another, a layout that collapses at this width, an image that \
did not load. Specific and checkable, and these hold a release.

**guideline** -- the page does not meet a stated guideline. No display typeface, a \
palette that is a framework default rather than a choice, spacing with no system \
to it. Also checkable, also real, and the reason guidelines are written to be \
falsifiable.

**judged** -- your opinion as a designer. "This reads as templated", "the hierarchy \
fights the headline", "the hero says nothing". Valuable, and deliberately *not* \
blocking: somebody weighing whether to ship reads it alongside everything else. A \
department that could veto on taste would stop products shipping forever, because \
there is always something.

If the page is fine, say so. A review that always finds something is not a review, \
it is a tax.\
"""


async def write_guidelines_task(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Write how one product should look, and store it like acceptance criteria."""
    product_id = str(task.payload.get("product_id", ""))
    product = await repository.get_product(session, product_id) if product_id else None
    if product is None:
        return f"design: no product {product_id!r} to write guidelines for"

    idea = await repository.get_idea(session, product.idea_id)
    audience = idea.target_niche if idea else "unknown"
    problem = idea.problem_statement if idea else product.name

    try:
        guidelines = await ctx.llm.parse(
            DesignGuidelines,
            task_type=TaskType.ANALYSIS,
            system=GUIDELINES_SYSTEM,
            prompt=(
                f"Product: {product.name}\n"
                f"Who sees it: {audience}\n"
                f"What it solves: {problem}\n\n"
                f"The company baseline:\n\n{render_design_for_prompt(HOUSE_STYLE)}\n\n"
                "Write the design guidelines for this product."
            ),
            engine=EngineName.DESIGN,
            task_id=task.id,
            max_tokens=16_000,
        )
    except Exception as exc:
        log.warning("design.guidelines_failed", product_id=product_id, detail=str(exc)[:200])
        return f"design: could not write guidelines for {product_id} -- {str(exc)[:150]}"

    await repository.record_design_guidelines(
        session, product_id=product_id, guidelines=guidelines, source_task_id=task.id
    )
    log.info(
        "design.guidelines_written",
        product_id=product_id,
        must_hold=len(guidelines.must_hold),
        must_avoid=len(guidelines.must_avoid),
    )
    return f"design: guidelines written for {product.name}"


async def review_design_task(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Open the deployed page, look at it, and judge it against its guidelines."""
    from factory.llm.providers.base import Image
    from factory.tools.browser import BrowserSession, allowed_origins_for

    product_id = str(task.payload.get("product_id", ""))
    product = await repository.get_product(session, product_id) if product_id else None
    if product is None or not product.deploy_url:
        return f"design: product {product_id!r} has no deployed page to look at"

    guidelines = await repository.live_design_guidelines(session, product_id) or HOUSE_STYLE

    origins = allowed_origins_for(product.deploy_url, product.custom_domain)
    if not origins:
        return f"design: {product_id} has no reviewable origin"

    # Both widths, because a layout that holds on a desktop and collapses on a
    # phone is the single most common real defect and is invisible at one size.
    shots: list[Image] = []
    browser = BrowserSession(allowed_origins=origins)
    try:
        async with browser as tools:
            opener = tools.get("browser_open")
            shutter = tools.get("browser_screenshot")
            if opener is None or shutter is None:  # pragma: no cover - registry is fixed
                return f"design: browser tools unavailable for {product_id}"
            await opener.handler(url=product.deploy_url)
            for width in (1280, 390):
                taken = await shutter.handler(width=width)
                payload = taken.get("_images") or []
                if payload:
                    shots.append(Image(data=payload[0]["data"]))
    except Exception as exc:
        log.warning("design.could_not_look", product_id=product_id, detail=str(exc)[:200])
        return f"design: could not open {product.deploy_url} -- {str(exc)[:150]}"

    if not shots:
        # A review that could not see must never read as a review that found
        # nothing. Same rule as everywhere else today: a check that did not happen
        # is not a check that passed.
        log.warning("design.no_screenshot", product_id=product_id)
        return f"design: took no screenshot of {product_id}; not reviewed"

    try:
        review = await ctx.llm.parse(
            VisualReview,
            task_type=TaskType.VISUAL_REVIEW,
            system=REVIEW_SYSTEM,
            prompt=(
                f"This is {product.name}, live at {product.deploy_url}. The first "
                "image is a desktop viewport, the second a phone.\n\n"
                f"{render_design_for_prompt(guidelines)}\n\n"
                "Judge what you can see."
            ),
            images=shots,
            engine=EngineName.DESIGN,
            task_id=task.id,
            max_tokens=16_000,
        )
    except Exception as exc:
        log.warning("design.review_failed", product_id=product_id, detail=str(exc)[:200])
        return f"design: could not review {product_id} -- {str(exc)[:150]}"

    blocking = 0
    for finding in review.findings:
        # `judged` is recorded and weighed, never blocking. A design department
        # that can veto on taste is the unbounded-findings gate with a new name.
        severity = {"broken": "high", "guideline": "medium", "judged": "low"}.get(
            finding.kind, "low"
        )
        if severity == "high":
            blocking += 1
        await repository.record_issue(
            session,
            product_id=product_id,
            kind="ui",
            severity=severity,
            title=f"[{finding.kind}] {finding.summary}",
            location=f"design: {finding.where[:100]}" if finding.where else "design: page",
            actual=finding.evidence,
            found_by=f"design:{finding.kind}",
        )

    await repository.record_task_event(
        session,
        event="design.reviewed",
        task_id=task.id,
        product_id=product_id,
        engine=EngineName.DESIGN,
        detail={
            "verdict": review.verdict,
            "findings": len(review.findings),
            "blocking": blocking,
            "summary": review.summary[:800],
        },
    )
    log.info(
        "design.reviewed",
        product_id=product_id,
        verdict=review.verdict,
        findings=len(review.findings),
        blocking=blocking,
    )
    return (
        f"design: reviewed {product.name} -- {review.verdict}, "
        f"{len(review.findings)} finding(s), {blocking} blocking"
    )
