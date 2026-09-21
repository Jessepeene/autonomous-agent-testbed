"""Does this claim refer to something the claimant was actually given?

A worker that reports on specific items -- criteria, leads, findings, accounts,
channels -- names them in prose, and prose can name anything. When the name resolves
to nothing it was handed, the report is not a statement about the world. It is a
statement about the report, and it has to be read as one.

**The general failure, found the expensive way.** A gap reviewer was handed one block
containing acceptance criteria, deliberate non-goals, and standing policy, under a
heading calling all of it "the acceptance criteria this product was built to meet".
It reported two MUSTs as unmet. One was a policy line, word for word: *"the page does
not know its own final address... a custom domain is attached afterwards, by a
person"* -- a sentence explaining why something is fine, filed as a defect. The other
was an out-of-scope entry, a thing deliberately not built, reported as missing.

Both were unsatisfiable by construction, so two repair cycles moved nothing, and the
release decision correctly refused to spend a third and escalated to a human. The
model was not careless: it was told those lines were criteria and it graded them.
`GAP_SYSTEM` says *"Never invent a criterion. Judge only what you were given"*, which
is an instruction in a prompt standing in for a check in code -- and this codebase has
now paid for that substitution more than once.

**Why this is its own module rather than five lines in the gap check.** Three places
already do a narrow version of it by hand: `supervisor/product_decision.py` drops an
`Unmet` it does not recognise, `learning/feedback.py` drops a capability the company
does not have, and now this. All three are the same question, and the number of places
asking it only goes up as more of this company's internal traffic becomes one part of
it naming something to another. An autonomous organisation that cannot tell "I found a
problem with X" from "I have invented an X" accumulates work that cannot be finished.

**What grounding is not.** It does not judge whether a claim is *correct* -- only
whether its subject exists. A grounded claim can still be wrong; an ungrounded one
cannot be assessed at all, because there is nothing to assess it against. And a claim
that grounds to nothing is not therefore worthless: it is often a worker noticing
something real that the standard never mentioned, which is exactly the observation a
narrow gate is blind to. So the answer here is *where a claim belongs*, never *whether
to keep it*. Deciding what to do with one that belongs nowhere is the caller's, and
throwing it away is rarely the right answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from factory.engines.discovery.cluster import tokenize

#: Below this, two texts are not the same statement. Overlap is measured as the
#: share of the *shorter* text's tokens that appear in the longer one, not Jaccard:
#: the texts being compared are routinely a truncation, a paraphrase, or a quote with
#: the surrounding sentence attached, and Jaccard reads all three as different
#: statements because it is punished by length alone. The real live case was a
#: 150-character truncation of a 300-character policy line -- every token present,
#: Jaccard 0.5, obviously the same sentence.
DEFAULT_THRESHOLD = 0.6

#: A claim shorter than this cannot be matched on overlap, because a handful of
#: common words will coincide with almost anything. Such a claim is reported as
#: ungrounded rather than matched to whichever entry happened to share two nouns.
MIN_TOKENS = 3


@dataclass(frozen=True, slots=True)
class Grounded:
    """Where a claim's subject was found, if anywhere."""

    #: Name of the group the subject came from -- for a work standard, which of
    #: `must`/`should`/`out_of_scope`/`policy`. `None` when nothing matched.
    group: str | None
    #: The catalogue entry itself, so a caller can use the canonical wording rather
    #: than the claimant's rendering of it.
    entry: str | None
    #: How strongly it matched, for logs and for a caller wanting to be stricter.
    score: float

    @property
    def found(self) -> bool:
        return self.group is not None


def overlap(left: str, right: str) -> float:
    """Share of the shorter text's meaningful tokens that the longer one also has.

    Deliberately asymmetric-tolerant: a quote is a subset of its source, a truncation
    is a prefix of the whole, and both should read as the same statement. What it
    must not do is call two different statements the same because they are both about
    landing pages, which is what the stopword-stripped token sets are for.
    """
    a, b = tokenize(left, include_numeric=True), tokenize(right, include_numeric=True)
    if len(a) < MIN_TOKENS or len(b) < MIN_TOKENS:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def ground(
    claim: str,
    catalogue: Mapping[str, Sequence[str]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> Grounded:
    """Which group of `catalogue` the claim's subject came from, if any.

    `catalogue` is ordered by the caller and ties break toward the earlier group, so
    a caller lists its groups in the order it wants ambiguity resolved. For a work
    standard that is `must` before `should` before `out_of_scope` before `policy`:
    an entry restated in two places is most consequential as the strictest of them.

    Returns a `Grounded` whose `group` is `None` when the claim matches nothing --
    never an exception and never a guess, because "I could not place this" is a real
    answer and the caller is the one that knows what it means.
    """
    best = Grounded(group=None, entry=None, score=0.0)
    for group, entries in catalogue.items():
        for entry in entries:
            score = overlap(claim, entry)
            if score >= threshold and score > best.score:
                best = Grounded(group=group, entry=entry, score=round(score, 3))
    return best
