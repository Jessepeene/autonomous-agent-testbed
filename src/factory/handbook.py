"""The employee handbook — what a worker can look up before deciding anything.

The gap this fills, in Jesse's own framing: *"if I'm a dev and I get told to
implement a new feature I know what the product is about, I can read
documentation, I know the purpose of the product so I can gauge why it would be
important and how to implement it… and I document what I do and justify my
actions."*

None of that was available. An employee here could read its own system prompt,
whatever memory recall surfaced, and — only if it was a repair role — one
product's `.factory/ARCHITECTURE.md`. It could not discover who else worked here,
what happened to its work after handoff, what rules bound it, or what the product
it was editing was actually *for*. It optimised blind against gates it could not
see, which is exactly how a repair agent came to run the test suite, watch it
pass, and conclude there was nothing to do while eight review findings sat
unaddressed.

**Everything here is generated from the source of truth, never hand-written.**
Hand-written documentation drifts, and drifted documentation is worse than none
because an employee acts on it confidently — the same reason the living-record
principle (`ARCHITECTURE_ROADMAP.md`) insists a record be checkable against real
rows. The org directory is derived from `ROLES`; the policy brief from the
settings and gates that actually enforce it; a product dossier from its own rows.
If the code changes, the handbook changes with it.

Pull, not push: these are exposed as tools (`tools/handbook.py`) so an employee
looks something up when it needs it, rather than every prompt carrying a wiki it
mostly will not read.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.agents.roles import CALL_SITE_SCOPED_ROLES, ROLES
from factory.facts import Unmet, product_facts
from factory.settings import Settings, get_settings

#: **What is not known, stated as plainly as what is.**
#:
#: A brief listing only what exists cannot distinguish "nobody has researched this
#: audience" from "this audience is thoroughly understood" — both produce a brief with
#: no audience section, and a decider reading it has no way to tell. Every gap found
#: across five live runs had that shape: a silent default reaching a decision as though
#: it were a finding.
#:
#: The *facts* live in `factory.facts`; these are one reader's phrasing of them. A
#: manager scanning ten products needs a sentence, so the sentence is written here
#: rather than in the fact itself — a different reader (a dashboard column, a test, a
#: future Product department deciding what to commission) needs the name, not the
#: prose, and would otherwise have to parse English back into a predicate.
_UNMET_PROSE: dict[Unmet, str] = {
    Unmet.AUDIENCE: "nobody has established who this is for beyond one inferred sentence",
    Unmet.REQUIREMENTS: "nobody has written what this product must do",
    Unmet.DESIGN_GUIDELINES: "nobody has written how it should look",
    Unmet.DEPLOYMENT: "it has never been deployed, so nobody has looked at it",
    Unmet.REACH: "nobody outside the company has seen it",
}

#: How departments actually hand work to each other, and who owns what.
#:
#: Curated rather than derived: the real map lives in `enqueue_task(engine=...)`
#: calls scattered across five engines, and reconstructing it by static analysis
#: would be fragile in exactly the way that produces confidently wrong docs.
#: `tests/unit/test_handbook.py` asserts every department named here is a real
#: `EngineName`, so this cannot drift into describing departments that do not
#: exist.
HANDOFF_MAP = """\
How work moves through this company:

  Discovery ──(an idea worth assessing)──▶ Compliance
  Compliance ──(an approved idea, a product row)──▶ Development
  Design ──(how this product should look)──▶ Development
  Development ──(a page or product that is live)──▶ Design, Marketing
  Marketing ──(enough real outreach to judge interest)──▶ Development

Departments and what they own:

- **Discovery** finds evidence that a real unmet need exists, and proposes ideas.
  It never decides whether an idea is legal or buildable.
- **Compliance** decides whether an idea may be pursued at all, and assesses every
  individual lead's lawful basis before anyone is contacted. Its "no" is final.
- **Development** builds, reviews, ships and repairs products. Nothing reaches a
  customer without passing its verification gate.
- **Design** decides how a product should look for the people who will see it, and
  judges a deployed page by *looking* at it -- the only department that can, and the
  reason it exists: everything else reviews source, and a page can be correct in
  every line and still read as machine-generated. Its findings are weighed by the
  release decision; it deliberately cannot veto on taste.
- **Marketing** sources leads, gets human approval for a campaign, sends outreach,
  and measures what came back.
- **Finance** reconciles real money and enforces the spending limit. It is
  deliberately the one department with no judgement in it at all.
- **Supervisor** decides what the company works on next, and is the only part that
  talks to the human operator.

What happens to your work after you hand it off matters: a build is not finished
when the code is written, it is finished when it passes the test suite, then Bug
Council review, then deploys. Optimising for the step in front of you while
ignoring the gate behind it is the most common way work here gets wasted.
"""


def org_directory() -> str:
    """Who works here and what each of them can actually do.

    Generated from `ROLES` so it can never disagree with the roster. Before this,
    no employee could discover that any other existed -- so none could ever say
    "that is not mine, it belongs to X".
    """
    lines = [
        "Everyone who works at this company, and what they are for.",
        "",
        "You are one of these. The others exist; you cannot call them directly, but",
        "knowing who owns what tells you when a problem is not yours to solve.",
        "",
    ]
    for name, role in ROLES.items():
        reach = "only from its own call site" if name in CALL_SITE_SCOPED_ROLES else "on request"
        acts = "can change things" if role.mutating else "read-only"
        lines.append(f"- **{name.value}** ({acts}, reachable {reach})")
        lines.append(f"    {role.purpose}")
        lines.append(f"    Tools: {', '.join(role.tool_categories)}")
    return "\n".join(lines)


def policy_brief(settings: Settings | None = None) -> str:
    """The rules that bind every employee, stated once in words.

    Sourced from the settings and gates that actually enforce them, so the numbers
    here are the numbers in force. An employee that does not know a spend cap
    exists cannot reason about why its expensive approach was cut off.
    """
    settings = settings or get_settings()
    cap = settings.daily_llm_spend_cap_usd
    # The cap is genuinely optional (`float | None`), and a handbook that formats
    # `None` as a number would be exactly the confidently-wrong documentation this
    # module exists to avoid. Say which of the two is true instead.
    spend = (
        f"Aggregate model spend is limited to ${cap:.2f} across a rolling 24 hours; "
        "past it, every generative task stops until spend ages out."
        if cap is not None
        else "No aggregate spend cap is configured right now, so only your own run's "
        "cost ceiling applies."
    )
    return f"""\
Rules that apply to everyone here. These are enforced in code, not by trust, and
none of them are yours to override.

- **Spending is capped.** {spend}
  Your run also has its own cost and step ceiling. Being cut off mid-task is a
  budget decision, not a judgement about your work.
- **Nobody is contacted without a lawful basis.** Every lead passes an individual
  Legitimate Interest Assessment, and every campaign needs an explicit human
  approval before a single message is sent. A suppression-list match blocks a send
  outright, and a missing suppression list blocks *everyone* — it fails closed.
- **Generated code never runs on the host.** It runs in a sandbox with no network.
  If you cannot install something, that is deliberate and not a fault to work
  around.
- **Nothing ships unreviewed.** Tests, then a deterministic contract and SEO check,
  then Bug Council, then deploy. A human can override this for their own product;
  you cannot.
- **Some prompts can be tuned by the company and some never can.** Legal
  assessment, code review, the orchestrator and real product code generation are
  permanently excluded from self-modification.
- **Say when you are blocked.** If you cannot proceed because something you were
  told you had is missing, report that plainly rather than working around it. A
  blocked worker that stays silent is indistinguishable from one that tried and
  failed, and the company cannot fix what it never hears about.
- **Document what you did and why.** The next person to touch this — quite possibly
  you, weeks later, with no memory of today — has only what you wrote down.
"""


def _traction_reading(status: str, interest: int, reach: int) -> str:
    """Say what the two numbers mean, so a reader cannot mistake silence for failure.

    Zero interest after a thousand emails and zero interest after none are opposite
    findings that look identical as a number, and the wrong reading is the expensive
    one: it retires a product nobody was ever told about, or keeps one real people
    have already declined.
    """
    if reach == 0 and interest == 0:
        return (
            "Nobody has been told about it yet, so zero interest is not evidence "
            "against it -- it is the absence of evidence either way."
        )
    if interest == 0:
        return (
            f"It has been put in front of {reach} people and none confirmed "
            "interest. That is a real signal, not a gap in the record."
        )
    return f"Real people want this: {interest} confirmed after {reach} approach(es)."


async def product_dossier(session: AsyncSession, product_id: str) -> str:
    """Everything known about one product, assembled from its own rows.

    The information already existed and no one could read it: the plan lives on
    `Idea`, the look and feel on `Product.design_brief`, what has been tried in
    `heal_attempts`, real interest in `validation_signals`, and reach in
    `outreach_log` — five places, assembled by nobody. Employees got a single
    flattened summary string instead, which is why work happened without anyone
    knowing what the product was for.

    Every line is derived from a row. Nothing here is recalled or inferred.
    """
    from factory.state import repository

    pair = await repository.get_product_with_idea(session, product_id)
    if pair is None:
        return f"No product with id {product_id!r}."
    product, idea = pair

    lines = [
        f"# {product.name}",
        "",
        f"Status: {product.status}. Live at: {product.deploy_url or 'not deployed yet'}.",
        "",
        "## What this product is for",
        f"Problem: {idea.problem_statement}",
        f"Who it is for: {idea.target_niche}",
        f"How it is meant to make money: {idea.monetization_hypothesis}",
        f"Evidence this need is real: {idea.evidence_count} corroborating signal(s), "
        f"found at {idea.source_url}",
    ]
    if idea.self_serve_price_usd:
        lines.append(f"Intended price: ${idea.self_serve_price_usd}")

    # Promised by this function's own docstring since it was written, and never
    # rendered. The gap was not cosmetic: "is anyone actually interested in this?"
    # is the question every decision about a product turns on, and its absence is
    # what a department manager blocked on -- it could not establish whether a
    # product mattered, so it guessed that it did.
    #
    # Counts, never rows. `ValidationSignal.email` and `Lead.work_email` are real
    # addresses; nobody reading a dossier needs them, and a brief that carries PII
    # around the company is a minimisation problem rather than a briefing.
    interest, reach = await repository.product_traction(session, product_id)
    lines += [
        "",
        "## Whether anyone actually wants it",
        f"Confirmed interest: {interest} signal(s) from real visitors who "
        "clicked through the confirmation email.",
        f"Reach: {reach} outreach email(s) sent about it.",
        _traction_reading(product.status, interest, reach),
    ]

    if product.design_brief:
        lines += ["", "## Look and feel", product.design_brief[:800]]

    history = await repository.product_history(session, product_id, limit=40)
    if history:
        lines += ["", "## What has happened to it, in order"]
        lines += [
            f"- {e.created_at:%Y-%m-%d %H:%M} {e.event}"
            + (f" — {e.detail.get('reason') or e.detail.get('note', '')}"[:160] if e.detail else "")
            for e in history[-20:]
        ]

    # What this product is held to, ahead of everything except what it is for.
    # An employee that knows the requirements can tell a defect from a thing that
    # was never meant to exist -- and can tell "done" from "perfect", which the
    # must/should split is there to make sayable.
    requirements = await repository.live_requirements(session, "product", product_id)
    if requirements is None:
        # Stated, not omitted. `decide_release` already tells its judge this exact
        # fact outright ("No acceptance criteria were written for this product, so
        # there is no stated standard to judge it against"); an agent reading the
        # dossier used to get silence, and silence reads as "nothing to see".
        lines += [
            "",
            "## What it must do",
            "**Nobody has written this down.** There is no stated standard for this "
            "product, so nothing here can tell you whether it is finished. Treat any "
            "judgement about 'done' as your own, and say that you made it.",
        ]
    else:
        lines += ["", "## What it must do (the standard it is judged against)", requirements.goal]
        lines += [f"- MUST: {c.get('criterion', '')}" for c in requirements.must]
        lines += [f"- SHOULD: {c.get('criterion', '')}" for c in requirements.should]
        if requirements.out_of_scope:
            lines += [
                "- OUT OF SCOPE (deliberately absent, not defects): "
                + "; ".join(requirements.out_of_scope)
            ]

    # The backlog, before the repair history: what is wrong with this product right
    # now is more useful to an employee about to touch it than what was tried
    # before, and `times_seen` is the line that says "this one keeps coming back".
    open_issues = await repository.open_issues_for(session, product_id)
    if not open_issues:
        # "Nothing is wrong with it" and "nobody has looked" are different facts, and
        # an absent section is how they became one.
        reviewed = any(e.event.startswith(("council", "release", "gaps")) for e in history)
        lines += [
            "",
            "## Known open issues",
            "None recorded."
            + (
                ""
                if reviewed
                else " Nothing has ever reviewed this product, so read "
                "that as 'unexamined' rather than 'clean'."
            ),
        ]
    else:
        lines += ["", "## Known open issues"]
        lines += [
            f"- [{i.severity}] {i.title}"
            + (f" (at {i.location})" if i.location else "")
            + f" — {i.status}, seen {i.times_seen}x since {i.first_seen_at:%Y-%m-%d}"
            for i in open_issues[:12]
        ]

    attempts = await repository.heal_attempts_for(session, product_id)
    if attempts:
        lines += ["", "## Repairs already attempted (do not repeat these blindly)"]
        lines += [
            f"- attempt {a.attempt_number} [{a.outcome}]: {a.healing_text[:200]}"
            for a in attempts[-6:]
        ]

    return "\n".join(lines)


async def product_brief(session: AsyncSession, product_id: str) -> str:
    """One product in a few lines: enough to judge whether it deserves attention.

    The scanning counterpart to `product_dossier`, which is the file you open once
    you have decided a product matters. A manager looking across ten products cannot
    read ten files, and handing it ten files is how a prompt stops being read at all.

    What goes in is what changes a "is this worth my time" answer: who it is for,
    whether anyone wants it, whether there is anything to look at, and whether it is
    already known to be broken. Everything else is a reason to open the dossier.
    """
    facts = await product_facts(session, product_id)
    if facts is None:
        return f"- {product_id}: no such product."

    # Derived once, in `facts.product_facts`, so that every reader gets the same answer
    # to "does this have design guidelines" -- the builder and the reviewer holding
    # slightly different derivations of the same question is how a page came to be
    # built to one standard and judged against another.
    lines = [
        f"- {facts.product_id}  {facts.name}",
        f"    for: {facts.audience_sentence}",
        f"    status: {facts.status}, "
        + (f"deployed at {facts.deploy_url}" if facts.is_deployed else "nothing deployed"),
        f"    wanted: {facts.interest} confirmed interest signal(s) after "
        f"{facts.reach} approach(es)"
        + (
            " -- nobody has been told about it yet"
            if facts.reach == 0 and facts.interest == 0
            else ""
        ),
        f"    known problems: {facts.open_issues} open issue(s)",
    ]
    if facts.unmet:
        lines.append(f"    NOT known: {'; '.join(_UNMET_PROSE[u] for u in facts.unmet)}")
    # A tally its own rows cannot support is not a fact about the product, so it is
    # stated separately rather than folded into `NOT known`: a decider told "2 pieces
    # of evidence" should know it cannot read any of them.
    if not facts.evidence_count_is_trustworthy:
        lines.append(
            f"    CAUTION: the idea claims {facts.claimed_evidence_count} piece(s) of "
            f"evidence but {facts.audience_evidence} are stored, so nothing downstream "
            "can read what it rests on"
        )
    return "\n".join(lines)


async def brief_products(session: AsyncSession, product_ids: list[str], *, limit: int = 10) -> str:
    """Briefs for the products a decision is about. "" when there are none.

    Deliberately here and not in any one department's code. Every manager has the
    same problem -- it is asked to judge what is worth doing to a set of products and
    is given only their names -- so the fix belongs where every manager can reach it,
    not in the first department that needed it.

    Bounded, because a manager's domain can grow without limit and a brief nobody
    reads is worse than no brief: it displaces the part of the prompt that mattered.
    """
    unique = list(dict.fromkeys(pid for pid in product_ids if pid))[:limit]
    if not unique:
        return ""
    briefs = [await product_brief(session, pid) for pid in unique]
    return "What is actually known about these products, from their own records:\n\n" + "\n".join(
        briefs
    )
