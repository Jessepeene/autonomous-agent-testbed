"""Rewriting the confirmation email or the page its link leads to, in response to
one specific, already-filed finding.

The other half of `factory/signup_journey.py`. That module renders; this one writes
what gets rendered, and the two are kept in separate files on purpose -- a bug in
composing new copy must never be mistaken for a bug in falling back to old copy.

**Reactive, not proactive.** Every product starts with `signup_journey == {}` and
stays that way until a reviewer -- human or agent -- actually flags the generic
version as a problem (`tools/product_review.py`'s `surface` fork is what gets a
finding here instead of to a code repair). This was the deliberate choice among three
real options: bundle it into every landing-page build, give it its own commissioned
ownership like `DESIGN_GUIDELINES`, or write it only when someone has a reason to want
it changed. The third costs every other product nothing, and it is how the underlying
problem was found in the first place -- not a rule generating work in advance, but a
person actually using the product and noticing the confirmation page looked wrong.

**A structured judgement, not a spawned agent.** There is no code to run and no
filesystem to act in -- the whole job is deciding what one or two short pieces of text
should say, which is exactly the shape `write_landing_page_requirements` and
`decide_release` already use.

**The door this opens, held to the same policy the landing page already answers to.**
This is the first place agent-authored text reaches a real person who gave a real
email address through a channel that is not the reviewed, gated landing page itself.
`SIGNUP_JOURNEY_POLICY` exists because that risk texture is different from a page
nobody has signed anything on yet, and it should be named as a rail here rather than
inherited by accident from wherever this code happened to be written.
"""

from __future__ import annotations

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.signup_journey import (
    CONFIRMATION_EMAIL_BODY,
    CONFIRMATION_EMAIL_SUBJECT,
    CONFIRMATION_PAGE_TEXT,
)
from factory.state import repository
from factory.state.orm import Task
from factory.state.schemas import Contract
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

#: Standing policy for every piece of copy this module writes, regardless of which
#: finding prompted it. Not `PAGE_POLICY` verbatim -- that prompt is about a page
#: that sells the product and can defer its own final address, neither of which is
#: true of a confirmation screen -- but it holds the same line: no fabricated
#: evidence, no internal scaffolding, nothing claimed that is not true.
SIGNUP_JOURNEY_POLICY = """\
STANDING POLICY. This overrides anything in the finding below that disagrees with it.

- Say who this is and what they signed up for, in the product's own voice -- never \
generic system language ("Your submission has been received"), and never internal \
scaffolding (no mention of validation, testing, this being an experiment, or how \
the page came to exist).
- No invented evidence. No testimonials, customer counts, or claims about the \
product that are not already established by its own name and what it does.
- Never promise a specific reply time, a specific person, or a next step this \
company cannot actually guarantee.
- A working link to delete their data is added automatically after your text. Do \
not write your own -- one you invented would not be the real one, and the real one \
must always be the one a visitor can actually click.
- Keep it short. This is a confirmation, not a pitch -- one or two sentences reading \
as a person, not a form.\
"""

SIGNUP_JOURNEY_SYSTEM = f"""\
You write the confirmation email or confirmation page for one product, in response \
to a specific problem a reviewer found with the generic, factory-wide version.

You are given the finding, this product's name and what it does, and the current \
text for every piece of the journey (the product's own override if it has one \
already, otherwise the same generic wording every product starts with). Change \
only what the finding is actually about. A finding about the confirmation email \
gives you no reason to touch the confirmation page, and rewriting something the \
finding never mentioned is a second, uncommissioned change riding on the first.

Leave a field you are not changing set to null, not repeated -- a repeated value \
looks like a decision when it is actually silence, and the merge this feeds treats \
null as "nothing to say here" correctly only if you use it that way.

{SIGNUP_JOURNEY_POLICY}

Say in one sentence what you changed and why, for the record.\
"""


class SignupJourneyPatch(Contract):
    """What changed, in response to one finding. Any field left `None` is not
    touched -- see `update_signup_journey`'s merge, which depends on that."""

    confirmation_email_subject: str | None = None
    confirmation_email_body: str | None = None
    confirmation_page_text: str | None = None
    rationale: str = Field(min_length=10)


_FIELD_KEYS = {
    "confirmation_email_subject": CONFIRMATION_EMAIL_SUBJECT,
    "confirmation_email_body": CONFIRMATION_EMAIL_BODY,
    "confirmation_page_text": CONFIRMATION_PAGE_TEXT,
}


async def patch_signup_journey(ctx: FactoryContext, session: AsyncSession, task: Task) -> str:
    """Handle one `PATCH_SIGNUP_JOURNEY` task: read the finding, judge what should
    change, write it. Returns a summary string for the task log."""
    product_id = task.product_id or str(task.payload.get("product_id", ""))
    product = await repository.get_product(session, product_id) if product_id else None
    if product is None:
        return f"development: product {product_id!r} not found for signup-journey patch"

    current = product.signup_journey
    default = "(factory default)"
    prompt = (
        f"Product: {product.name}\n\n"
        "The finding a reviewer filed:\n"
        f"  {task.payload.get('finding_summary', '')}\n"
        f"  {task.payload.get('finding_details', '')}\n"
        f"  (seen at: {task.payload.get('finding_location', '') or 'not given'})\n\n"
        "The current text for each piece of the journey, this product's own "
        "override if it already has one:\n"
        f"  confirmation_email_subject: {current.get(CONFIRMATION_EMAIL_SUBJECT) or default}\n"
        f"  confirmation_email_body: {current.get(CONFIRMATION_EMAIL_BODY) or default}\n"
        f"  confirmation_page_text: {current.get(CONFIRMATION_PAGE_TEXT) or default}\n"
    )

    try:
        patch = await ctx.llm.parse(
            SignupJourneyPatch,
            task_type=TaskType.ANALYSIS,
            system=SIGNUP_JOURNEY_SYSTEM,
            prompt=prompt,
            engine=EngineName.DEVELOPMENT,
            task_id=task.id,
        )
    except Exception as exc:
        log.warning("signup_journey.patch_failed", product_id=product_id, detail=str(exc)[:200])
        return f"development: signup-journey patch for {product_id} failed -- {str(exc)[:150]}"

    changed = {
        _FIELD_KEYS[field]: value
        for field, value in patch.model_dump().items()
        if field in _FIELD_KEYS and value is not None
    }
    if not changed:
        log.info("signup_journey.no_change", product_id=product_id, rationale=patch.rationale[:200])
        return (
            f"development: signup-journey patch for {product_id} changed nothing -- "
            f"{patch.rationale[:150]}"
        )

    await repository.update_signup_journey(session, product_id, **changed)
    log.info(
        "signup_journey.patched",
        product_id=product_id,
        fields=list(changed),
        rationale=patch.rationale[:200],
    )
    return f"development: signup-journey for {product_id} updated ({', '.join(changed)})"
