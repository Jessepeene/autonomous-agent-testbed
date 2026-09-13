"""Writing down what a piece of work is for, before anyone starts it.

The step a human team never skips and this factory never had. Somebody says what
the thing must do and what would make it done; everything after that -- building,
reviewing, testing, deciding whether to ship -- refers back to it.

Without it every downstream judgement was unanchored, and it showed:

- Bug Council reviews against categories (security, performance, quality) and never
  against intent, so it cannot say "this does not do what it was for".
- AUDITOR had to infer the product's purpose by looking at the product, which is
  circular -- it can only ever find things that look wrong, never things that are
  missing.
- Tests cover whatever the generator chose to cover.
- Release was `if not deploy_blocking`, which is unbounded: there is always another
  finding, so "done" was a state the system could not reach.

The requirements themselves are generated, not hand-written, from the idea the
business already approved -- problem, audience, price, monetisation. That keeps
them checkable against something real (`ARCHITECTURE_ROADMAP.md`'s living-record
principle) rather than being prose invented alongside the work they judge.

A note on why this is not just a longer checklist: `must` / `should` /
`out_of_scope` exist so that **done and perfect are different states**. A flat list
of requirements is another blocking gate, and the old deploy gate already showed
where that ends.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.schemas import ValidatedIdea, WorkRequirements
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

LANDING_PAGE_REQUIREMENTS_SYSTEM = """\
You write the acceptance criteria for a landing page before anyone builds it.

This page exists for one purpose: to find out whether real people want a product \
that does not exist yet. It is not the product. Its job is to describe the offer \
honestly enough that a stranger's decision to leave an email means something.

Write criteria that someone can *check by using the page*. "A visitor can submit an \
email address and sees a confirmation telling them to check their inbox" is \
checkable. "Good user experience", "professional design" and "fast" are not -- a \
reviewer cannot fail them honestly, so they make every judgement that cites them \
unfalsifiable.

must: the things that, if any one is missing, mean this page cannot do its job. \
Keep this list short and genuinely non-negotiable. Most pages need four to six. \
Include what the page has to *say* (the specific problem, who it is for, the real \
price) as well as what it has to *do*, because a page that works perfectly and \
describes the wrong offer has still failed.

should: real improvements that are not worth blocking a launch over. This is what \
someone deciding whether to release weighs against the cost of waiting.

out_of_scope: things a reviewer might otherwise flag as missing. A validation page \
has no accounts, no dashboard, no working product behind it, and no payment -- \
naming that stops a review demanding them and stops a repair agent building them.

Never invent a feature, a claim, or a price the idea does not support. If the idea \
does not state a price, do not require the page to show one.\
"""


async def write_landing_page_requirements(
    ctx: FactoryContext,
    session: AsyncSession,
    *,
    product_id: str,
    idea: ValidatedIdea,
    task_id: str | None = None,
) -> WorkRequirements | None:
    """Produce and store the acceptance criteria for one product's landing page.

    Returns ``None`` if they could not be written -- the caller proceeds anyway
    rather than blocking a build on it. That is deliberate: requirements make every
    downstream judgement better, and a build with none is exactly what the factory
    did until today, so failing to write them is a degradation, not an outage.
    """
    existing = await repository.live_requirements(session, "product", product_id)
    if existing is not None:
        return None

    prompt = (
        "The business has approved this idea and is about to build a landing page "
        "to test whether anyone wants it.\n\n"
        f"Problem it claims to solve: {idea.problem_statement}\n"
        f"Who it is for: {idea.target_niche}\n"
        f"How it would make money: {idea.monetization_hypothesis}\n"
        f"Evidence the need is real: {idea.evidence_count} corroborating signal(s), "
        f"found at {idea.source_url}\n"
        + (
            f"Intended price: ${idea.self_serve_price_usd} per month\n"
            if idea.self_serve_price_usd
            else "No price has been decided.\n"
        )
        + "\nWrite the acceptance criteria for that page."
    )

    try:
        requirements = await ctx.llm.parse(
            WorkRequirements,
            # Judgement over a brief, not code generation -- so it runs on whatever
            # serves ANALYSIS rather than the expensive code path.
            task_type=TaskType.ANALYSIS,
            system=LANDING_PAGE_REQUIREMENTS_SYSTEM,
            prompt=prompt,
            engine=EngineName.DEVELOPMENT,
            task_id=task_id,
        )
    except Exception as exc:  # noqa: BLE001 - degradation, never a build blocker
        log.warning("requirements.not_written", product_id=product_id, detail=str(exc)[:200])
        return None

    await repository.record_requirements(
        session,
        subject_kind="product",
        subject_id=product_id,
        requirements=requirements,
        created_by="development:landing_page",
        source_task_id=task_id,
    )
    log.info(
        "requirements.written",
        product_id=product_id,
        must=len(requirements.must),
        should=len(requirements.should),
    )
    return requirements


def render_for_prompt(requirements: WorkRequirements) -> str:
    """The requirements as an instruction block, for whoever is doing the work.

    One rendering, shared by the builder, the reviewer and the release decision, so
    three employees cannot end up working to three different readings of the same
    standard.
    """
    lines = [f"What this must achieve: {requirements.goal}", "", "MUST (not done without these):"]
    lines += [f"- {c.criterion}  ({c.why})" for c in requirements.must]
    if requirements.should:
        lines += ["", "SHOULD (valuable, does not block a release):"]
        lines += [f"- {c.criterion}  ({c.why})" for c in requirements.should]
    if requirements.out_of_scope:
        lines += ["", "OUT OF SCOPE (deliberately not built -- do not add or flag these):"]
        lines += [f"- {item}" for item in requirements.out_of_scope]
    return "\n".join(lines)
