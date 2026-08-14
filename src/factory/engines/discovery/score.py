"""Idea generation and discrimination.

Two separate model calls with opposing incentives: the generator is asked to propose,
the discriminator is asked to kill. Running them as one call produces ideas that
grade their own homework.
"""

from __future__ import annotations

from factory.engines.discovery.cluster import Cluster
from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state.schemas import DiscriminatedIdeas, IdeaBatch, ScoredIdea
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

_GENERATOR_KEY = "factory.engines.discovery.score:GENERATOR_SYSTEM"
_DISCRIMINATOR_KEY = "factory.engines.discovery.score:DISCRIMINATOR_SYSTEM"
_WEIGHTS_KEY = "factory.engines.discovery.score:SCORING_WEIGHTS"

#: Below this, an idea is not worth an engineer's time — even a synthetic one.
SCORE_THRESHOLD = 0.55

#: Distribution, not code generation, is the factory's bottleneck: a product that
#: needs a human to sell or onboard it does not fit an operation with no sales team,
#: whatever its score. Module-level constant, not a `Settings` field, matching
#: `SCORE_THRESHOLD` immediately above — both are product-strategy decisions, not
#: deployment configuration.
PLG_PRICE_CEILING_USD = 200.0

GENERATOR_SYSTEM = """\
You identify B2B micro-SaaS opportunities from raw public complaints.

You are given clusters of real posts where people describe an unmet need. For each \
cluster that represents a genuine, recurring, *paid-solvable* business problem, emit \
one idea.

Rules:
- problem_statement describes the customer's pain in their terms, not a product pitch.
- target_niche names a specific buyer role and company type, not "businesses" or \
"developers". "Bookkeepers at 5-20 person Dutch accounting firms" is a niche; \
"finance teams" is not.
- source_url must be one of the URLs shown in that cluster. Never invent one.
- Skip clusters that are consumer problems, one-person annoyances, ideas requiring a \
regulated licence, or needs already well served by an obvious incumbent.

Emitting fewer, better ideas is correct. An empty list is a valid answer.\
"""

DISCRIMINATOR_SYSTEM = """\
You are the discriminator. Your job is to kill weak ideas before anyone builds them, \
and to attach a concrete monetization hypothesis to the survivors.

For each idea, score four independent dimensions from 0.0 to 1.0:
- pain_severity: how badly this hurts today. Workarounds that cost hours weekly score \
high; mild annoyances score low.
- willingness_to_pay: whether this buyer has budget authority and an existing habit of \
paying for tools. Score low when the sufferer cannot authorise spend.
- build_feasibility: whether a small team could ship a useful v1 in days, using public \
APIs, with no proprietary data and no regulated activity.
- adoption_ease: whether this buyer can actually start using and paying for new \
software alone, independent of pain or price. Score high when the buyer already \
discovers and buys similar self-serve tools online without anyone's approval. Score \
low for industries with long procurement cycles, safety or compliance sign-off \
requirements, low tech adoption, or a cultural preference for phone calls and paper \
over self-serve checkout -- construction, healthcare, government, and heavy industry \
are common examples. A high-pain, high-budget idea in a low-adoption-ease industry is \
still a bad bet: the sale never closes regardless of how real the pain is.

monetization_hypothesis must name who pays, roughly how much per month, and what \
triggers the purchase. "Subscription" is not a hypothesis.

This factory has no sales team and no onboarding staff: every product must sell and \
onboard itself. For every idea also set:
- requires_human_sales: true if closing a paying customer plausibly needs a sales \
call, a demo, or price negotiation rather than a self-serve checkout.
- requires_human_onboarding: true if a customer cannot start getting value alone from \
signup, without a human walking them through setup, migrating their data, or \
configuring something on their behalf.
- self_serve_price_usd: the monthly price a self-serve buyer would pay without talking \
to anyone, in whole US dollars.

Score honestly and low. Optimistic scoring here wastes real engineering budget \
downstream. If you would not personally build it, say so in rejection_reason and score \
it accordingly.\
"""


async def generate_ideas(
    ctx: FactoryContext,
    clusters: list[Cluster],
    *,
    system: str = GENERATOR_SYSTEM,
    tunable_parameter_id: str | None = None,
    task_id: str | None = None,
    memory_block: str = "",
) -> IdeaBatch:
    if not clusters:
        return IdeaBatch(ideas=[])

    rendered = "\n\n".join(
        f"### Cluster {index + 1} "
        f"(signals={cluster.size}, engagement={cluster.engagement})\n{cluster.summary()}"
        for index, cluster in enumerate(clusters[:12])
    )
    # memory_block goes in the *prompt*, never `system`: `system` is the tunable
    # value this call is served under (see learning/prompts.py::get_prompt), and
    # appending prior-run context to it would mutate a versioned prompt outside
    # `learning/store`'s own promotion path, corrupting trial attribution for
    # GENERATOR_SYSTEM. Prefixed, not appended, so it reads as context before
    # the actual task rather than an afterthought.
    prefix = f"{memory_block}\n\n" if memory_block else ""
    return await ctx.llm.parse(
        IdeaBatch,
        task_type=TaskType.DISCOVERY,
        system=system,
        prompt=f"{prefix}Signal clusters:\n\n{rendered}\n\nEmit ideas.",
        engine=EngineName.DISCOVERY,
        task_id=task_id,
        tunable_parameter_id=tunable_parameter_id,
    )


async def discriminate(
    ctx: FactoryContext,
    batch: IdeaBatch,
    *,
    system: str = DISCRIMINATOR_SYSTEM,
    tunable_parameter_id: str | None = None,
    task_id: str | None = None,
    memory_block: str = "",
) -> DiscriminatedIdeas:
    if not batch.ideas:
        return DiscriminatedIdeas(ideas=[])

    rendered = "\n\n".join(
        f"### Idea {index + 1}\n"
        f"problem_statement: {idea.problem_statement}\n"
        f"target_niche: {idea.target_niche}\n"
        f"source_url: {idea.source_url}"
        for index, idea in enumerate(batch.ideas)
    )
    # Same reasoning as generate_ideas above -- memory in the prompt, never system.
    prefix = f"{memory_block}\n\n" if memory_block else ""
    return await ctx.llm.parse(
        DiscriminatedIdeas,
        task_type=TaskType.ANALYSIS,
        system=system,
        prompt=f"{prefix}Candidate ideas:\n\n{rendered}\n\nScore each one.",
        engine=EngineName.DISCOVERY,
        task_id=task_id,
        tunable_parameter_id=tunable_parameter_id,
    )


def passes_plg_constraint(
    idea: ScoredIdea, *, price_ceiling_usd: float = PLG_PRICE_CEILING_USD
) -> bool:
    """Deterministic admission gate, run *after* the discriminator scores the idea —
    same pattern as the LIA's ``passed`` field or the rules-based lead gates: the
    model self-reports the inputs, code makes the binding decision, so no amount of
    prompt-level persuasion can talk an idea past this. All three conditions must
    hold; any one PLG violation is disqualifying regardless of how strong the score."""
    return (
        not idea.requires_human_sales
        and not idea.requires_human_onboarding
        and idea.self_serve_price_usd <= price_ceiling_usd
    )


def survivors(
    result: DiscriminatedIdeas,
    weights: dict[str, float] | None = None,
    threshold: float = SCORE_THRESHOLD,
) -> list[ScoredIdea]:
    """``weights`` defaults to the fixed historical weights (via ``idea.score``) so
    every caller that never heard of the learning loop behaves exactly as before."""
    scored = weights if weights is not None else ScoredIdea.default_weights()
    scored_ok = [idea for idea in result.ideas if idea.weighted_score(scored) >= threshold]
    kept = [idea for idea in scored_ok if passes_plg_constraint(idea)]
    log.info(
        "discovery.discriminated",
        candidates=len(result.ideas),
        scored_survivors=len(scored_ok),
        survivors=len(kept),
        plg_rejected=len(scored_ok) - len(kept),
        threshold=threshold,
    )
    return sorted(kept, key=lambda i: i.weighted_score(scored), reverse=True)
