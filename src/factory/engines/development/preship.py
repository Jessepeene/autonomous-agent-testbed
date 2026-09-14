"""Use the product before calling it shipped.

Every other judge in this factory reads source. The gap check compares files to
acceptance criteria, Bug Council reviews code in six categories, the generated
test asserts against the HTML on disk, and the contract and SEO checks grep for
strings. All five are real and none of them can answer the only question a
customer asks: *does it work?*

The proof is the bug that prompted this. There was no CORS configuration anywhere
in the API, so the browser's mandatory preflight for a cross-origin JSON POST got
a bare 405 and the fetch was never sent. Every form on every landing page this
factory has ever deployed failed for every visitor, and the interest signal the
entire validation stage is built on could not arrive. Every source reviewer passed
it, because the markup is correct. `curl` passed it, because `curl` sends no
`Origin` and needs no preflight. The defect existed only in the gap between
correct source and a working product, and nothing in the pipeline stood there.

Two halves, and the split is the point:

**The agent decides what to do.** Open the page, work out what it offers, try to
do that thing, read the copy the way the intended buyer would. Judgement, and it
needs a browser.

**Deterministic code decides whether it worked.** `BrowserSession` records every
request the page made; a failed one is a fact, not an opinion. Asking an agent
"did the form work?" and believing the answer is precisely the trust gap this
review exists to close, so the caller never has to hope the agent mentioned it.

`tools/browser.py` has existed for this the whole time, with a docstring naming
this exact failure -- "a signup form that silently fails on submit". It was only
ever pointed at products in `LIVE`, by a periodic sweep. Validation pages sit in
`VALIDATING` and are the only thing this factory currently ships, so the one
reviewer that could have caught this never ran on the one product type that
needed it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import RoleName, categories_for, get_role
from factory.api.app import SELF_TEST_SUFFIX
from factory.engines.development.requirements import render_for_prompt
from factory.llm.providers.base import LLMError
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.schemas import PreShipReview, WorkRequirements
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: The address the reviewer submits. `/interest/{product_id}` answers it exactly
#: like a real submission -- same status, same body, same CORS envelope -- but
#: records nothing and sends nothing, so a self-test can never inflate the count a
#: build-or-scrap decision is gated on or bounce against the sending reputation.
SELF_TEST_EMAIL = f"preship-review{SELF_TEST_SUFFIX}"

REVIEW_SYSTEM = """\
You are the last person to look at a product before it goes in front of real \
buyers. You have a real browser and the page is really deployed.

Do these two things, in order.

**1. Try to do what the page is for.** Find the thing it asks a visitor to do -- \
usually leaving an email address -- and actually do it. Fill the field, submit it, \
and then establish whether it *worked*. Watch the network activity each action \
reports: a submission that fires no request, or one that fails, has not worked, \
however encouraging the page looks afterwards. A thank-you message proves nothing \
on its own; pages show those before the request comes back.

**2. Read it as the buyer it names.** You are a sceptical professional deciding \
in ten seconds whether this is real. Say what would stop you.

Specifically worth catching, because a code reviewer structurally cannot:

- Internal scaffolding that leaked into customer-facing copy: references to the \
system that built the page, provenance for where the idea came from, links to \
issue trackers or research sources, placeholder text, notes to self.
- The page contradicting itself, or saying something the product does not do.
- Copy that reads like a description of an experiment rather than a product.

Report only what you actually observed. "The form may not work" is not a finding; \
"I entered an address, clicked Join, and the request to /interest failed" is. If \
something is fine, say it is fine -- a list of everything imperfect is how a \
product never ships, and cosmetic is a real category you are allowed to use.\
"""


@dataclass(slots=True)
class PreShipOutcome:
    """What the review concluded, and whether it may ship.

    `ran` distinguishes "reviewed and found nothing" from "could not review", which
    must never look the same. A review that could not run is not a pass.
    """

    ran: bool
    blocking: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    summary: str = ""

    @property
    def may_ship(self) -> bool:
        return not self.blocking


def _failed_requests(observed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Requests the page made that did not succeed.

    The deterministic half. A `FAILED (...)` entry is a request the browser refused
    or could not complete -- a CORS block looks exactly like this -- and a 4xx/5xx
    is a request the server rejected. Neither is a matter of opinion, and neither
    depends on the agent having noticed.
    """
    failures = []
    for entry in observed:
        status = entry.get("status")
        if isinstance(status, str) and status.startswith("FAILED"):
            failures.append(entry)
        elif isinstance(status, int) and status >= 400:
            failures.append(entry)
    return failures


def _describe(entry: dict[str, Any]) -> str:
    return (
        f"{entry.get('method', '?')} {entry.get('url', '?')} -> {entry.get('status')}"
        + (f"\n{entry['body']}" if entry.get("body") else "")
    )


def _split_by_primary_action(
    failures: list[dict[str, Any]], *, product_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate "the page cannot do its job" from "something else 404'd".

    The interest endpoint carries the product id, so this is an exact match on the
    one request that has to work rather than a guess about which URLs look
    important. Everything else -- an analytics beacon, a missing icon, a font --
    is recorded and weighed, because a page whose favicon is missing has still
    collected the email address it exists to collect.
    """
    primary: list[dict[str, Any]] = []
    incidental: list[dict[str, Any]] = []
    for entry in failures:
        url = str(entry.get("url", ""))
        (primary if f"/interest/{product_id}" in url else incidental).append(entry)
    return primary, incidental


async def review_before_ship(
    ctx: FactoryContext,
    session: AsyncSession,
    *,
    product_id: str,
    product_name: str,
    deploy_url: str,
    custom_domain: str | None = None,
    requirements: WorkRequirements | None = None,
    task_id: str | None = None,
) -> PreShipOutcome:
    """Open the deployed page, use it, and judge whether it may ship."""
    from factory.agents.runtime import AgentRuntime
    from factory.tools.browser import BrowserSession, allowed_origins_for
    from factory.tools.handbook import build_handbook_tools

    origins = allowed_origins_for(deploy_url, custom_domain)
    if not origins:
        log.warning("preship.no_reviewable_origin", product_id=product_id, url=deploy_url[:120])
        return PreShipOutcome(ran=False, summary="no reviewable origin")

    role = get_role(RoleName.AUDITOR)
    objective = (
        f"The page is live at {deploy_url}\n\n"
        f"It is meant to be: {product_name}\n\n"
        "Open it and follow your instructions. When you submit the form, use "
        f"exactly this address: {SELF_TEST_EMAIL}\n"
        "It is a reserved self-test address -- the endpoint answers it exactly like "
        "a real submission but records nothing, so you can test the form honestly "
        "without creating a fake signup."
    )
    if requirements is not None:
        objective += (
            "\n\nThis page was built to meet the criteria below. Anything listed as "
            "out of scope is absent on purpose and is not a defect.\n\n"
            + render_for_prompt(requirements)
        )

    browser = BrowserSession(allowed_origins=origins)
    try:
        async with browser as tools:
            runtime = AgentRuntime(
                ctx.llm,
                settings=ctx.settings,
                tools=build_handbook_tools(
                    role=role.name.value,
                    task_id=task_id,
                    product_id=product_id,
                    engine=EngineName.DEVELOPMENT,
                    base=tools,
                ),
            )
            result = await runtime.run(
                objective=objective,
                system=REVIEW_SYSTEM,
                tool_categories=categories_for(role),
                task_type=role.task_type,
                engine=EngineName.DEVELOPMENT,
                task_id=task_id,
                max_steps=role.max_steps,
                deadline_seconds=role.deadline_seconds,
                docker_available=ctx.docker_available,
                cost_cap_usd=role.cost_cap_usd,
                max_context_chars=role.max_context_chars,
            )
    except Exception as exc:
        # Playwright missing, a browser that would not start, a navigation that
        # hung. A review that could not run must not read as a pass, but it must
        # also not strand a product that deployed successfully -- the caller
        # decides, and `ran=False` is how it knows the difference.
        log.warning("preship.review_failed", product_id=product_id, detail=str(exc)[:200])
        return PreShipOutcome(ran=False, summary=f"review could not run: {str(exc)[:200]}")

    # The deterministic half, read from the session rather than the transcript.
    #
    # Only a failure of the thing the page exists for is decided here. Everything
    # else is evidence, and the difference matters: the first version of this
    # blocked on *any* failed request, so a 404 on a missing favicon or an og-image
    # would have held a release with no judgement applied at all. That is the same
    # unbounded-findings gate the release judgement was built to replace, smuggled
    # back in one layer down.
    #
    # The submission endpoint is different in kind. A validation page that cannot
    # record an email does not do the one thing it was built for, so this is a
    # stated requirement failing, not a defect to be weighed -- the same line
    # `must` already draws.
    failures = _failed_requests(browser.observed_requests)
    primary, incidental = _split_by_primary_action(failures, product_id=product_id)

    blocking = [_describe(entry) for entry in primary]
    notes = [_describe(entry) for entry in incidental]

    outcome = PreShipOutcome(
        ran=True, blocking=blocking, notes=notes, summary=result.answer[:2000]
    )

    if not result.answer.strip():
        # Same failure `_review_live_product` hit twice live: a budget spent
        # investigating with no final turn to extract from. The deterministic half
        # above still stands on its own, which is most of why it exists.
        log.info("preship.no_prose", product_id=product_id, outcome=result.summary())
        return outcome

    try:
        review = await ctx.llm.parse(
            PreShipReview,
            task_type=TaskType.ANALYSIS,
            system=(
                "Extract what the reviewer observed into the schema. Record only "
                "what it actually reported doing and seeing; never infer a finding "
                "it did not make, and never soften one it did."
            ),
            prompt=f"The reviewer's own account:\n\n{result.answer[:8000]}",
            engine=EngineName.DEVELOPMENT,
            task_id=task_id,
            max_tokens=16_000,
        )
    except LLMError as exc:
        log.warning("preship.extract_failed", product_id=product_id, detail=str(exc)[:200])
        return outcome

    outcome.summary = review.summary or outcome.summary
    if review.primary_action_attempted and not review.primary_action_succeeded:
        outcome.blocking.append(
            "The reviewer could not complete the action the page exists for: "
            f"{review.summary[:400]}"
        )
    for finding in review.findings:
        line = f"[{finding.kind}] {finding.summary}" + (
            f" — {finding.evidence}" if finding.evidence else ""
        )
        # Only `broken` holds a release here. "This does not work" is a fact and
        # belongs with the deterministic half; "a buyer would not trust this" is a
        # judgement, and there is already a judge for it -- the release decision,
        # which weighs findings against what the product is for and can accept one
        # deliberately. Blocking on `credibility` from here overruled that judge
        # with a hardcoded rule, which is the shape of gate the whole release
        # design replaced.
        #
        # Nothing is lost by recording instead of blocking: every finding becomes
        # an issue below, `open_issues_for` feeds the next release decision, and a
        # credibility problem serious enough to matter is one a judge weighing "is
        # this good enough to put in front of a real person" will act on.
        (outcome.blocking if finding.kind == "broken" else outcome.notes).append(line)

    for line in outcome.blocking:
        await repository.record_issue(
            session,
            product_id=product_id,
            kind="bug",
            severity="high",
            title=line[:300],
            location=f"deployed: {deploy_url[:100]}",
            found_by="preship_review",
        )
    for line in outcome.notes:
        await repository.record_issue(
            session,
            product_id=product_id,
            kind="ui",
            severity="low",
            title=line[:300],
            location=f"deployed: {deploy_url[:100]}",
            found_by="preship_review",
        )

    # A durable marker that this stage ran, for `audit.check_pipeline_completeness`.
    # A stage that silently stops running is invisible otherwise: the gap check files
    # nothing when it finds nothing, so "found no problems" and "never executed" leave
    # identical traces. That is how a design brief went unbuilt for months.
    await repository.record_task_event(
        session,
        event="preship.reviewed",
        task_id=task_id,
        product_id=product_id,
        engine=EngineName.DEVELOPMENT,
        detail={
            "blocking": len(outcome.blocking),
            "notes": len(outcome.notes),
            "network_failures": len(failures),
            "action_succeeded": review.primary_action_succeeded,
        },
    )
    log.info(
        "preship.reviewed",
        product_id=product_id,
        blocking=len(outcome.blocking),
        notes=len(outcome.notes),
        network_failures=len(failures),
        action_succeeded=review.primary_action_succeeded,
    )
    return outcome
