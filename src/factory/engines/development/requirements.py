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

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.memory import store as memory_store
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.schemas import (
    AcceptanceCriterion,
    DesignGuidelines,
    ValidatedIdea,
    WorkRequirements,
)
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

LANDING_PAGE_REQUIREMENTS_SYSTEM = """\
You write the acceptance criteria for a landing page before anyone builds it.

This page exists for one purpose: to find out whether real people want a product \
that has not been built yet. It is not the product.

**Write criteria about THIS product only.** A standing policy already covers what \
every page must and must not do -- present-tense product framing that sells, no \
invented evidence, nothing internal to this factory on the page, and what the \
canonical URL may be before a domain is attached. It is attached to every build and \
every review automatically, so you neither need to restate it nor may contradict it.

Two kinds of criterion are always wrong here, and both have failed real builds:

- Anything about how this company builds, deploys, hosts or reviews a page. \
Canonical URLs, domains, placeholder hostnames, the deployment target, test \
coverage, the review process. A build was blocked for three repair cycles by \
"the canonical URL should point to the real deployed domain" -- which the page \
cannot possibly satisfy, because the domain is attached after deployment by a \
person. None of that is a fact about this product and none of it is yours.
- Anything restating or reversing the standing policy. Another build failed on \
"the page contains no claim that the product exists", which contradicts the policy \
requiring exactly that claim. You were given the policy so you would not have to \
guess at it; guessing produced the opposite of it.

If a rule would be true of every page this factory ever builds, it is policy and \
not yours to write.

What belongs here is everything specific to this idea: the problem this page has to \
name, who it must speak to, the actual price, and what a visitor has to be able to \
do.

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
        # `source_url` and `evidence_count` are deliberately NOT passed. They were,
        # labelled as internal and with an explicit instruction not to cite them,
        # and the model wrote "SHOULD: the page links to the real public issue
        # thread used as the need signal" anyway -- which put the scraped-from URL
        # in front of customers, because a build is judged against its criteria.
        #
        # Jesse's call, and it is the right shape of fix: not a better warning, just
        # don't hand it over. Nothing here needs it. Criteria describe what the page
        # must say and do; where the idea came from cannot inform any of that, so
        # passing it only creates a way to get this wrong.
        + (
            f"Intended price: ${idea.self_serve_price_usd} per month\n"
            if idea.self_serve_price_usd
            else "No price has been decided.\n"
        )
        + "\nWrite the acceptance criteria for that page."
    )

    # What this company has already been told about work of this kind. Until now
    # nothing that *writes* recalled anything: two reviewers, the heal loop and agent
    # spawns all did, and every one of them reads work that is already finished. So a
    # standard this company learned could only ever arrive as a complaint about a
    # built page, never as a criterion before the build -- which is the difference
    # between an organisation that learns and one that has to be told twice.
    #
    # Here rather than in the builder's own prompt, because criteria are read three
    # times: the builder works to them, the pre-ship reviewer judges against them, and
    # the release decision weighs them. A standard that becomes a criterion reaches
    # all three. One injected into the build prompt reaches one.
    recalled = await memory_store.recall_block(
        session,
        query=(
            f"landing page for {idea.target_niche}: what the page must say and do, "
            "the signup, the confirmation mail it sends, the page that link leads to"
        ),
        scopes=memory_store.scopes_for(engine=EngineName.DEVELOPMENT, product_id=product_id),
    )
    if recalled:
        prompt += (
            "\n\nStandards this company has learned, from people who have looked at "
            "work like this before. They are not criteria yet -- turning them into "
            "criteria is your judgement. Apply the ones that bear on this page, and "
            "ignore any that do not.\n\n" + recalled
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
    except Exception as exc:
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


#: Standing rules for every validation page, applied live rather than baked into a
#: product's criteria. This is the permanent fix for a whole class of problem, so it
#: is worth being explicit about why it exists.
#:
#: These are not facts about any one product. "Never publish where the idea came
#: from" is true of every page this factory will ever build, and it changes when the
#: business changes its mind, not when a product does. Generated criteria are the
#: opposite: written once from one idea, frozen for that build, and correct to store.
#:
#: Mixing them cost a day. The requirements written for one product included
#: "SHOULD: the page links to the real public issue thread used as the need signal"
#: -- a standing-policy question, answered by a generator that had not been told the
#: policy, and then treated as authoritative because criteria are what a build is
#: judged against. Three consequences, all structural: the policy was re-decided per
#: product (so it varied), frozen at generation time (so fixing the prompt did
#: nothing to pages already specified), and could be got *wrong* by the generator
#: -- which is what happened, producing a criterion that contradicted an explicit
#: instruction in the builder's own prompt.
#:
#: Rendered alongside the criteria on every read, and marked as overriding them, so
#: a stale criterion cannot win against current policy and a policy change takes
#: effect on the next build with nothing to regenerate.
PAGE_POLICY = """\
STANDING POLICY. These apply to every page this factory builds and override any \
criterion below that disagrees with them. A criterion asking for something this \
policy forbids, or forbidding something it requires, is a mistake in the criterion: \
the policy wins, the page is not failing, and **you must not report it as unmet**. \
Say the criterion contradicts policy instead.

- Nothing internal to this factory appears on the page. Not where the idea came \
from, not the research or discussion it was drawn from, not links to issue \
trackers or forums, not the validation process itself. A visitor sees a company \
describing its product, with no trace of how the page came to exist.
- No invented evidence. No testimonials, customer names or logos, user or customer \
counts, case studies, review scores, awards or press mentions -- each is a \
fabricated claim about real people.
- **This page sells the product.** Describe it in the present tense the way any \
company describes what it sells, make the case for it properly, and state the price \
plainly. Never hedge its existence, never tell the visitor it is not built yet, and \
never describe the page as a test or an experiment. A page that argues for the \
product and a page that measures demand are the same page: hedged copy does not \
produce an honest result, it produces a weak one that nobody responds to.
- The one thing a visitor can actually do is leave an email address, so that is what \
the call to action asks for. There is no checkout behind it, and a Buy button that \
cannot complete a purchase is the single claim here that would be dishonest rather \
than merely confident.
- The page does not know its own final address. It is deployed to a Vercel \
subdomain first and a custom domain is attached afterwards, by a person, so a \
canonical URL or og:url that is not the eventual public domain is expected and \
correct at build time. It is never a defect in the page and never something to \
repair.\
"""


def render_for_prompt(requirements: WorkRequirements, *, policy: str = PAGE_POLICY) -> str:
    """The standard as an instruction block, for whoever is doing or judging the work.

    One rendering, shared by the builder, the gap check, the live review and the
    pre-ship review, so four employees cannot end up working to four different
    readings of the same standard -- and so that changing a standing rule reaches all
    four at once without regenerating anything.

    **Every section says what force it carries, because the absence of that cost two
    repair cycles and a human.** This used to emit four kinds of statement -- a goal,
    graded criteria, deliberate non-goals, and standing policy -- as one undifferentiated
    block, and `gaps.py` handed the whole thing over under the heading "the acceptance
    criteria this product was built to meet". So a reviewer graded the policy: it
    reported *"the page does not know its own final address... a custom domain is
    attached afterwards, by a person"* as an unmet MUST, which is a sentence explaining
    why something is fine, filed as a defect. It also reported an out-of-scope entry as
    missing. Neither could ever be satisfied, so repair cycled without moving and the
    release decision correctly gave up and escalated.

    The four kinds differ in what a *departure* from them means, which is the thing a
    worker needs and could not previously recover: a MUST absent means not done, a
    SHOULD absent is weighed, an out-of-scope item *present* is scope creep, and a
    policy line cannot be departed from by a work product at all -- it is the rule the
    criteria are read against.

    `policy` is a parameter rather than welded in because standing policy is not a
    landing-page idea. Every department that judges work against criteria has rules
    that sit above them, and they all belong in this position with this framing; the
    page policy is simply the first one written.
    """
    lines = [
        "THE STANDARD THIS WORK IS JUDGED AGAINST.",
        "",
        f"What this must achieve: {requirements.goal}",
        "",
        "MUST -- absent means the work is not done:",
    ]
    lines += [f"- {c.criterion}  ({c.why})" for c in requirements.must]
    if requirements.should:
        lines += ["", "SHOULD -- weighed in the release judgement, never blocking:"]
        lines += [f"- {c.criterion}  ({c.why})" for c in requirements.should]
    if requirements.out_of_scope:
        lines += [
            "",
            "OUT OF SCOPE -- deliberately not built. These are not missing and must "
            "never be reported as unmet. Only their *presence* is a departure:",
        ]
        lines += [f"- {item}" for item in requirements.out_of_scope]
    if policy:
        lines += [
            "",
            "The section below is not part of the criteria above. It is standing "
            "policy: rules that hold for every piece of work of this kind and that "
            "override any criterion disagreeing with them. Nothing a work product "
            "does can make a policy line unmet, and no policy line is ever something "
            "to report as a gap, a defect, or something to repair.",
            "",
            policy,
        ]
    return "\n".join(lines)


def standard_catalogue(
    requirements: WorkRequirements,
    *,
    design: DesignGuidelines | None = None,
    policy: str = PAGE_POLICY,
) -> dict[str, list[str]]:
    """Every statement a worker was given, grouped by the force it carries.

    The counterpart to `render_for_prompt` and `render_design_for_prompt`: those hand
    a worker the standard, this lets a caller take what the worker said about it and
    find out which part it was actually talking about.

    **Everything rendered must appear here.** A statement a worker is graded against
    but that its report cannot be placed against gets treated as ungrounded, and the
    grounding check stops being a correction and becomes a mute button. Design is the
    live example: `must_hold` is graded by the same pass that grades `must`, so a real
    "no webfont, stock framework palette" gap -- the defect a human had to catch
    because no automated judge could -- would have been quietly downgraded to an
    observation by a change meant to stop over-blocking.

    The groups are the *force* of a statement, not the list it came from, which is why
    `must` and design's `must_hold` share one:

    - `must` / `must_hold` -- absent means not done. Blocks.
    - `should` -- absent is weighed, never blocking.
    - `must_avoid` -- present is a stated defect. Blocks. Not the same as scope creep:
      a page built from a framework's stock ramp is not a page that did extra work.
    - `out_of_scope` -- present is scope creep. Real, recorded, never blocking: a
      product that does more than it was asked is not one that fails its job.
    - `policy` -- not a criterion at all. The rule the criteria are read against, which
      nothing a work product does can leave unmet.

    Ordered strictest-first because `grounding.ground` breaks ties toward the earlier
    group, and a line restated in two places matters most as the strictest of them.
    """
    return {
        "must": [c.criterion for c in requirements.must]
        + (list(design.must_hold) if design else []),
        "must_avoid": list(design.must_avoid) if design else [],
        "should": [c.criterion for c in requirements.should],
        "out_of_scope": list(requirements.out_of_scope),
        # Bullets, not the whole block: a claim is placed against one statement, and
        # the policy's prose preamble is not one anybody can depart from.
        "policy": [
            line.lstrip("- ").strip()
            for line in policy.splitlines()
            if line.strip().startswith("- ")
        ],
    }


#: The house style every page is built and judged against until a Design department
#: exists to author one per product. In code rather than the database on purpose:
#: nobody writes it yet, and a stored artifact with no author is a table with one
#: row that drifts from the prompt beside it. When Design exists it produces these,
#: Development asks for them (`factory/comms.py`), and this becomes the fallback.
#:
#: What makes it different from the guidance that was already in the builder's
#: prompt is that it is *rendered into the gap check*, so a page can be failed
#: against it. Before this, "it looks machine-generated" was true, checkable by
#: eye, and invisible to every automated judge -- which is why a human had to catch
#: a page built from the Tailwind slate ramp with a navy gradient hero and no
#: webfont.
HOUSE_STYLE = DesignGuidelines(
    direction=(
        "Design for the buyer's own world rather than for 'a landing page'. A tool "
        "for CI engineers can borrow from terminal output, build logs, monospace "
        "timings and pipeline diagrams; one for accountants from ledgers and "
        "reconciliation. Specific and slightly unexpected beats tasteful and "
        "anonymous -- anonymous is what generated pages look like."
    ),
    palette=[
        "Choose 4-6 named hex values for this product and state them before building",
        "Pick the neutrals rather than inheriting them: a grey nudged toward the "
        "accent hue reads as chosen, a stock mid-grey reads as untouched",
        "Spend boldness in one place and keep everything around it quiet",
    ],
    typography=(
        "A characterful display face paired with a comfortable body face, both from "
        "Google Fonts with real fallback stacks. Set a type scale and hold to it; "
        "keep running text near 65 characters wide."
    ),
    must_hold=[
        "The page loads at least one webfont; a system-font-only stack is not a typographic choice",
        "The palette is specific to this product rather than a framework's default ramp",
        "Real, specific copy throughout -- never lorem-ipsum or placeholder text",
        "Legible on a phone: no horizontal scrolling, tap targets a thumb can hit",
    ],
    must_avoid=[
        "The Tailwind slate/gray ramp (#f8fafc, #64748b, #e2e8f0, #cbd5e1, #0f172a, "
        "#1e293b) or any other framework's stock palette",
        "A dark navy or purple-to-blue gradient hero",
        "-apple-system, Inter or Space Grotesk as the only typeface",
        "Emoji used as section markers",
        "Every section centered",
        "The same rounded-corner card treatment repeated down the page",
        "Three feature cards in a row with an icon above each",
    ],
)


def requirements_from_row(row: Any) -> WorkRequirements | None:
    """Rehydrate stored requirements into the shared contract.

    So the builder, the reviewer and a release decision all read one rendering of one
    standard, whether it was just written or read back from an earlier run.
    """
    try:
        return WorkRequirements(
            goal=row.goal,
            must=[AcceptanceCriterion(**c) for c in row.must],
            should=[AcceptanceCriterion(**c) for c in row.should],
            out_of_scope=list(row.out_of_scope),
        )
    except (TypeError, ValueError):  # pragma: no cover - a row written by an older shape
        return None


async def requirements_for_product(
    session: AsyncSession, product_id: str
) -> WorkRequirements | None:
    """What this product is held to. ``None`` when nobody has written it down yet.

    The chokepoint the docstring above has always claimed, and did not have. Every
    caller used to do its own `live_requirements` lookup and then decide for itself
    whether to pass the result on -- four of them, independently -- and one of the
    builders simply did not. So a regeneration was judged against criteria it had
    never been shown, and reported five of six MUSTs unmet for the honest reason that
    nobody told it what they were.

    Deliberately the same shape as `design_for_product`/`effective_design` beside it:
    a product's standards are resolved in one place and handed identically to whoever
    builds and whoever judges. A builder and a reviewer holding different copies of
    the standard is the failure both of these exist to make impossible.

    Note what this does *not* fix. Requirements are already general -- `product_dossier`
    renders them and `read_product_dossier` is a tool any agent can call. The
    generators cannot, because they are one-shot `parse()` calls rather than agent
    loops, so the only way a standard reaches them is a prompt somebody remembered to
    fill in. This makes the remembering structural; it does not give them eyes.
    """
    stored = await repository.live_requirements(session, "product", product_id)
    return requirements_from_row(stored) if stored else None


async def design_for_product(session: AsyncSession, product_id: str) -> DesignGuidelines | None:
    """What the Design department decided for this product. ``None`` if nobody wrote it.

    The missing half of a loop that was otherwise complete. Design could write
    guidelines and store them; `HOUSE_STYLE` was what the builder and the reviewer
    actually read, so a standard written for one specific product could never reach
    the page it was written for. Observed the day the department produced its first
    real guideline -- a palette, a type scale and a stated direction, stored
    correctly, and read by nothing.
    """
    # `live_design_guidelines` is typed `Any` because it reconstructs the contract
    # from the generic requirement columns it was stored in. Narrowed here rather
    # than cast, so a stored row that is not actually a `DesignGuidelines` degrades
    # to the house style instead of reaching a prompt as whatever it happens to be.
    stored = await repository.live_design_guidelines(session, product_id)
    return stored if isinstance(stored, DesignGuidelines) else None


def effective_design(product_design: DesignGuidelines | None) -> DesignGuidelines:
    """The standard a build is actually held to.

    A product's own guidelines when Design has written them, the house style when it
    has not -- and the distinction is not a fallback so much as a difference in kind.
    `HOUSE_STYLE` is *instructional*: "choose 4-6 named hex values and state them
    before building". A product's guidelines are the *decided answer*: these hex
    values, this typeface, this reason. So the specific one replaces the generic one
    rather than stacking on it, and they do not contradict because the specific one
    was written *from* the generic one.

    Both the builder and the reviewer resolve through here, which is the property
    that matters: a standard only works if everyone who acts on it reads the same
    text, and a page built to one standard and judged against another fails for
    reasons nobody can act on.
    """
    return product_design or HOUSE_STYLE


def render_design_for_prompt(guidelines: DesignGuidelines) -> str:
    """Design guidelines as an instruction block, for the builder and the reviewer.

    The same chokepoint shape `render_for_prompt` has, and for the same reason: a
    standard only works if everyone who acts on it reads the identical text.
    """
    lines = [
        "DESIGN GUIDELINES. The page is judged against these as well as its acceptance criteria.",
        "",
        f"Direction: {guidelines.direction}",
        "",
        "Palette:",
    ]
    lines += [f"- {item}" for item in guidelines.palette]
    lines += ["", f"Typography: {guidelines.typography}", "", "These must hold:"]
    lines += [f"- {item}" for item in guidelines.must_hold]
    if guidelines.must_avoid:
        lines += [
            "",
            "These must not appear -- each is a default rather than a decision, and "
            "together they are what a generated page looks like:",
        ]
        lines += [f"- {item}" for item in guidelines.must_avoid]
    return "\n".join(lines)
