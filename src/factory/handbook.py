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
from factory.settings import Settings, get_settings

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
  Development ──(a page or product that is live)──▶ Marketing
  Marketing ──(enough real outreach to judge interest)──▶ Development

Departments and what they own:

- **Discovery** finds evidence that a real unmet need exists, and proposes ideas.
  It never decides whether an idea is legal or buildable.
- **Compliance** decides whether an idea may be pursued at all, and assesses every
  individual lead's lawful basis before anyone is contacted. Its "no" is final.
- **Development** builds, reviews, ships and repairs products. Nothing reaches a
  customer without passing its verification gate.
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

    attempts = await repository.heal_attempts_for(session, product_id)
    if attempts:
        lines += ["", "## Repairs already attempted (do not repeat these blindly)"]
        lines += [
            f"- attempt {a.attempt_number} [{a.outcome}]: {a.healing_text[:200]}"
            for a in attempts[-6:]
        ]

    return "\n".join(lines)
