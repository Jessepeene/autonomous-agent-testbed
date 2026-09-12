"""Does an LLM's claim about a source actually appear in the real source text
it supposedly came from?

Generalizes `marketing/providers/website.py::_found_in_source` -- until now
the one place this pattern existed at all -- into shared infrastructure other
call sites with the identical hallucination risk can reuse instead of
reimplementing (Tier 1 finding #4, Tier 2 finding #9, system-wide
verification plan): an LLM extracting or quoting from a real document can
invent, embellish, or paraphrase a value that was never actually there, and a
deterministic substring check is a cheap, unambiguous backstop against it --
the same "a question with a checkable answer must not depend on a model's
mood" reasoning `development/contract.py`'s module docstring states outright.
"""

from __future__ import annotations


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def claim_in_source(claim: str, source: str) -> bool:
    """Whitespace/case-insensitive substring check -- for a value the model is
    expected to copy verbatim (a URL, an email address, a quoted sentence).
    Ported as-is from `marketing/providers/website.py::_found_in_source`,
    which pre-dates this module and still keeps its own name as a thin
    wrapper for compatibility with its own docstring/comments.
    """
    if not claim.strip():
        return False
    return _normalize(claim) in _normalize(source)


def claim_loosely_in_source(claim: str, source: str, *, min_word_length: int = 4) -> bool:
    """At least one distinctive word (length >= ``min_word_length``) from
    ``claim`` appears in ``source`` -- for a value the model is expected to
    *paraphrase* rather than copy verbatim (a job title: "Head of Finance" vs.
    the page's own "Finance Lead"), where `claim_in_source`'s exact match
    would false-negative on every real, correctly-extracted case. Fails
    closed: a claim with no word long enough to be distinctive (e.g. "CEO",
    "VP") never passes, the same direction every other fail-closed check in
    this codebase errs -- a real short title is rejected as unverified in
    that narrow case, rather than every claim silently passing when nothing
    about it can actually be checked.
    """
    normalized_source = _normalize(source)
    words = [word for word in _normalize(claim).split() if len(word) >= min_word_length]
    if not words:
        return False
    return any(word in normalized_source for word in words)
