"""Is there actually enough evidence here to justify building anything?

Jesse's question, and the system had no answer to it: *"the demand gets verified,
but isn't one post too little to verify a potential idea before building even a
landing page?"*

It was. Every idea this factory has ever produced carries ``evidence_count = 2``,
all five of them traced to the same GitHub thread, and the only gate on that
number anywhere is ``if evidence_count == 0``. Two comments cleared the bar to
spend real money on a build exactly as easily as two hundred would have.

The instinct to fix that with a threshold is the wrong one, and not only because
a number picked at a desk is arbitrary. Counting is the wrong *operation*. Ten
people saying "this would be nice" is weaker than one engineering lead describing
the workaround they maintain and what it costs them; a count cannot tell those
apart, and neither can a count with a bigger constant in front of it. What a
person does here is read the thread and decide whether the pain is real, specific,
and worth money.

So this reads the actual evidence. It could not have existed before
`idea_evidence` did -- the raw signals were deleted the moment an idea was written
from them, which is exactly why the only thing anyone could check was a number.

**Audience research is the same act, not a second feature.** Working out who has
this problem, what they already tried, and what language they use means reading
what they wrote. One pass over the same material answers both, and grounding the
audience in real quotes is what keeps it from being an invented persona -- which
is the failure mode a separate "write an audience brief" step would have walked
straight into.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.settings import EngineName, TaskType
from factory.state import repository
from factory.state.schemas import DemandAssessment
from factory.supervisor.context import FactoryContext

log = get_logger(__name__)

DEMAND_SYSTEM = """\
You decide whether there is enough real evidence of a problem to justify spending \
money finding out whether people will pay to have it solved.

You are shown the actual posts, issues and comments this idea was drawn from -- \
what people really wrote, not a summary. Read them.

**Weigh the evidence, do not count it.** Ten people saying "this would be nice" is \
weaker than one person describing the workaround they maintain, how long it takes, \
and what it costs them. What makes evidence strong:

- Specificity. Numbers, durations, tool names, concrete workarounds. Vague \
agreement is close to worthless.
- Cost to the person. Something that wastes an hour a week is a different \
proposition from something mildly annoying.
- Evidence they already pay, or already built something, to avoid the problem. \
That is the strongest signal available short of a sale.
- Independence. Several people in one thread agreeing is one data point about one \
community, not several data points.
- Recency. A problem from four years ago may already be solved.

**Then say who these people are**, in their own words -- their role, what they are \
responsible for, what they have already tried, and the vocabulary they use for the \
problem. This is what a landing page has to speak to, so specific beats flattering. \
Quote them where a quote says it better than a paraphrase.

Your verdict:

- **sufficient**: there is a real, specific, costly problem here and it is worth \
building a page to test demand.
- **thin**: something might be here, but the evidence does not yet support \
spending on it. Say exactly what would change your mind -- that is the useful \
part.
- **insufficient**: this does not show a problem worth solving.

Be willing to say thin or insufficient. Most ideas are not worth building, and a \
system that approves everything it is shown is not assessing anything. But do not \
be reflexively negative either: real evidence of a real problem is a yes.\
"""


async def assess_demand(
    ctx: FactoryContext,
    session: AsyncSession,
    *,
    idea_id: str,
    problem_statement: str,
    target_niche: str,
    task_id: str | None = None,
) -> DemandAssessment | None:
    """Read the evidence behind an idea and judge whether it justifies a build.

    ``None`` when there is nothing to read or the call fails -- the caller decides
    what that means. Deliberately not the same as "insufficient": no evidence
    retained is a fact about this factory, not about the idea, and conflating the
    two would retire every idea created before `idea_evidence` existed.
    """
    evidence = await repository.evidence_for_idea(session, idea_id)
    if not evidence:
        log.info("demand.no_evidence_retained", idea_id=idea_id)
        return None

    rendered = "\n\n".join(
        f"--- [{row.source}] {row.title} ({row.engagement} engagement)\n"
        f"{row.body[:3000]}\n{row.url}"
        for row in evidence[:12]
    )
    prompt = (
        f"Proposed idea: {problem_statement}\n"
        f"Believed audience: {target_niche}\n\n"
        f"The evidence it was drawn from -- {len(evidence)} signal(s), verbatim:\n\n"
        f"{rendered}"
    )

    try:
        assessment = await ctx.llm.parse(
            DemandAssessment,
            task_type=TaskType.ANALYSIS,
            system=DEMAND_SYSTEM,
            prompt=prompt,
            engine=EngineName.DISCOVERY,
            task_id=task_id,
            # Reads a dozen real posts and answers about all of them, then writes an
            # audience brief. The 8_192 ANALYSIS default is sized for neither, and
            # the release judgement already had to learn this the expensive way.
            max_tokens=24_000,
        )
    except Exception as exc:
        log.warning("demand.assessment_failed", idea_id=idea_id, detail=str(exc)[:200])
        return None

    log.info(
        "demand.assessed",
        idea_id=idea_id,
        verdict=assessment.verdict,
        signals=len(evidence),
        independent_sources=assessment.independent_sources,
    )
    return assessment
