"""What is established about a product, as facts rather than as a sentence.

`ProductStatus` answers *where a product is on the conveyor* — VALIDATING, BUILDING,
LIVE. It cannot answer what a product **has**, and every question worth asking is the
second kind. Two products in `VALIDATING`, one with design guidelines and one without,
are the same state to that model, which is exactly why nobody noticed the one that had
never been designed.

These predicates are not new. `handbook.product_brief` has derived them from rows since
Phase 2.7 and then joined them into an English sentence for a prompt — so an absence
was visible to a language model reading prose and to nothing else. It could not be
counted, waited on, reported or tested. That is the same shape as
`supervisor.audit.check_business_health` computing real concerns and stringifying their
count. This module changes only where the predicates live; the prose in `handbook` is
now a rendering of them rather than their only form.

**Nothing here decides anything.** `unmet` describes; it never dispatches. A resolver
that fired work at every unmet fact would be the conveyor rebuilt in a new notation,
with the ordering implicit in the dependency graph instead of written down — see
ARCHITECTURE_ROADMAP.md, Phase 2.8, "a manager decides; a resolver does not fire". What
to do about a missing standard is a judgement, including the judgement that the house
style is right for this product and nothing further is owed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.settings import EngineName
from factory.state.schemas import TaskKind


class Unmet(StrEnum):
    """Something a product does not have. A name, deliberately, and not a sentence:
    the sentence belongs to whoever is rendering it for a particular reader."""

    #: Nobody has anything real that people wrote about this problem. The audience is
    #: inferred from the idea's own generated prose and nothing else.
    AUDIENCE = "audience"
    #: No live requirements: nobody has written what this product must do.
    REQUIREMENTS = "requirements"
    #: No live design guidelines: nobody has decided how it should look, so a builder
    #: reaching for `effective_design` gets the house style with nothing saying so.
    DESIGN_GUIDELINES = "design_guidelines"
    #: Never deployed, so nobody inside or outside the company has looked at it.
    DEPLOYMENT = "deployment"
    #: Never shown to anyone outside. Zero interest here is the absence of evidence,
    #: not evidence of absence.
    REACH = "reach"


#: Which department can establish each fact, and how its manager is reached.
#:
#: **Structure, not sequence.** "Design decides how a product should look" is a fact
#: about this company and belongs in code. "Design runs after Compliance and before
#: Development" is the conveyor and belongs nowhere -- so this map says who owns a
#: question and never when they answer it. Adding a need should cost one entry here
#: and one predicate above; if it ever costs an edit to an ordering, the thing being
#: built is a pipeline again (ARCHITECTURE_ROADMAP.md, Phase 2.8).
#:
#: A need with no owner is left out deliberately rather than pointed at the nearest
#: department that could plausibly absorb it. `AUDIENCE` is the live case: nobody owns
#: "who is this for" today, Product/Management is where the roadmap puts it, and
#: assigning it to Design in the meantime would make an unowned question look answered
#: -- the exact failure this whole line of work exists to stop.
OWNER: dict[Unmet, tuple[EngineName, TaskKind]] = {
    Unmet.DESIGN_GUIDELINES: (EngineName.DESIGN, TaskKind.DESIGN_MANAGE),
}


@dataclass(frozen=True)
class ProductFacts:
    """Everything derivable about one product, each field read from rows.

    Frozen because these describe a moment. A caller that needs them again after doing
    something should ask again rather than carry a stale copy, which is the mistake
    `RoutingMemo` makes in the other direction.
    """

    product_id: str
    idea_id: str
    name: str
    status: str

    #: The one sentence Discovery wrote about who this is for. Always present, always
    #: inferred — `design/work.py` reads exactly this and calls it the audience.
    audience_sentence: str
    #: Rows in `idea_evidence`: what real people actually wrote. Zero means the
    #: audience sentence above rests on nothing observed.
    audience_evidence: int
    #: `Idea.evidence_count`, kept alongside the row count rather than in place of it.
    #: The two disagree in production — every idea carries 2 while the evidence table
    #: is empty — and holding both is how that stays visible instead of one silently
    #: standing in for the other. A value must say how it is known.
    claimed_evidence_count: int

    has_requirements: bool
    has_design_guidelines: bool
    deploy_url: str | None
    interest: int
    reach: int
    open_issues: int

    @property
    def is_deployed(self) -> bool:
        return bool(self.deploy_url)

    @property
    def audience_is_observed(self) -> bool:
        """Whether anyone established who this is for, beyond one generated sentence."""
        return self.audience_evidence > 0

    @property
    def evidence_count_is_trustworthy(self) -> bool:
        """Whether the idea's own evidence tally matches the evidence that exists.

        False is not a product problem — it is a claim the database makes about itself
        that its own rows do not support, and a decider told "2 pieces of evidence"
        should know it cannot read any of them.
        """
        return self.claimed_evidence_count == self.audience_evidence

    @property
    def unmet_signature(self) -> str:
        """A stable name for *this* configuration of absences.

        What makes a decision worth taking again. Keying the question on a clock means
        choosing an interval -- too short and a product is nagged about a situation
        nobody has changed, too long and work that finished hours ago goes unnoticed --
        and any interval chosen would be a number standing in for a judgement. Keyed on
        the absences instead, a decision is taken once per state of the product: the
        world changing *is* the trigger, and a world that has not changed asks nothing.
        """
        return "-".join(n.value for n in self.unmet) or "none"

    @property
    def unmet(self) -> tuple[Unmet, ...]:
        """What this product does not have, in the order the gaps compound.

        Audience first because everything downstream is written for somebody: a
        standard and a design decided without knowing the buyer are decided from the
        same generated sentence the idea started as.
        """
        missing: list[Unmet] = []
        if not self.audience_is_observed:
            missing.append(Unmet.AUDIENCE)
        if not self.has_requirements:
            missing.append(Unmet.REQUIREMENTS)
        if not self.has_design_guidelines:
            missing.append(Unmet.DESIGN_GUIDELINES)
        if not self.is_deployed:
            missing.append(Unmet.DEPLOYMENT)
        if self.reach == 0:
            missing.append(Unmet.REACH)
        return tuple(missing)


async def product_facts_for_open_products(
    session: AsyncSession, *, limit: int = 20
) -> list[ProductFacts]:
    """Facts for every product still in play, newest first.

    Retired is the one exclusion and it needs no judgement: nobody will look at it
    again. Bounded because a decision is taken per product per tick and an unbounded
    sweep is how a cheap check becomes the most expensive thing in the loop.
    """
    from factory.state.orm import Product
    from factory.state.schemas import ProductStatus

    rows = await session.execute(
        select(Product.id)
        .where(Product.status != ProductStatus.RETIRED.value)
        .order_by(Product.created_at.desc())
        .limit(limit)
    )
    out: list[ProductFacts] = []
    for product_id in rows.scalars().all():
        facts = await product_facts(session, product_id)
        if facts is not None:
            out.append(facts)
    return out


async def product_facts(session: AsyncSession, product_id: str) -> ProductFacts | None:
    """Read every fact about a product. ``None`` when there is no such product.

    One query set, one place: every reader gets the same answer to "does this have
    design guidelines" rather than each deriving it slightly differently, which is how
    the builder and the reviewer came to hold different standards for the same page.
    """
    from factory.state import repository

    pair = await repository.get_product_with_idea(session, product_id)
    if pair is None:
        return None
    product, idea = pair

    interest, reach = await repository.product_traction(session, product_id)
    return ProductFacts(
        product_id=product.id,
        idea_id=idea.id,
        name=product.name,
        status=str(product.status),
        audience_sentence=idea.target_niche,
        audience_evidence=len(await repository.evidence_for_idea(session, idea.id)),
        claimed_evidence_count=idea.evidence_count,
        has_requirements=(
            await repository.live_requirements(session, "product", product_id) is not None
        ),
        has_design_guidelines=(
            await repository.live_design_guidelines(session, product_id) is not None
        ),
        deploy_url=product.deploy_url,
        interest=interest,
        reach=reach,
        open_issues=len(await repository.open_issues_for(session, product_id)),
    )
