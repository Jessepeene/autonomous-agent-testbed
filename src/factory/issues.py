"""Giving a defect an identity.

This module answers one question: *is this the same bug we already know about?*

Getting it wrong in the permissive direction merges two real defects into one and
the second never gets fixed. Getting it wrong in the strict direction produces
what the factory did for 24 days -- one broken checkout filed four times, twice
inside the same minute, as four unrelated work items, rediscovered forever because
every review was a first encounter.

Two mechanisms, in order, because neither is sufficient alone:

1. **A deterministic fingerprint** over product + kind + normalised location. Cheap,
   stable, and exact when the reviewer names a real location (`POST
   /api/billing/checkout`, `#subscribe-btn`, `app/main.py:149`).
2. **Near-duplicate matching** on the wording, for when it does not. Reuses
   `jaccard`/`tokenize` at the same 0.6 threshold `memory/store.py` already uses to
   reinforce rather than re-add a near-identical lesson -- the same problem, and
   worth solving the same way rather than inventing a second notion of "close
   enough" in one codebase.

What is deliberately *not* here: asking a model whether two findings are the same.
That is the expensive, non-deterministic answer to a question two lines of set
arithmetic resolve, and BUG_LOG #20 is what happens when identity depends on an
LLM's phrasing being stable.
"""

from __future__ import annotations

import hashlib
import re

from factory.engines.discovery.cluster import jaccard, tokenize

#: Above this, two findings are treated as the same defect. Matches
#: `memory/store.py`'s near-duplicate bar deliberately -- see module docstring.
SAME_ISSUE_THRESHOLD = 0.6

#: The closed vocabulary. `improvement` and `feature_request` are not defects and
#: must never auto-commission a repair; they are recorded so a manager can see the
#: whole backlog rather than only what is broken.
KINDS = ("bug", "ui", "content", "regression", "improvement", "feature_request")
#: `feature_request` is here now, and was deliberately absent before. The reason it
#: was excluded was sound: a request has no "expected behaviour" to restore, so an
#: agent handed one would invent a specification and then build against its own
#: invention.
#:
#: What changed is that it no longer has to. An agent that cannot tell what was
#: meant reports RESULT: BLOCKED with a question, the work parks, and somebody who
#: knows answers it (`factory/comms.py`). The exclusion was a workaround for having
#: no way to ask; keeping it now would mean refusing work the organisation can
#: actually do.
#:
#: `improvement` stays out. It is not a request from anyone -- it is a reviewer
#: noticing something could be better, with no one to ask what "better" means.
ACTIONABLE_KINDS = ("bug", "ui", "content", "regression", "feature_request")

SEVERITIES = ("critical", "high", "medium", "low")

#: Statuses where the defect is believed to still be present.
OPEN_STATUSES = ("open", "triaged", "fixing")
#: Statuses where work is done but closure has not been earned yet.
PENDING_VERIFICATION = ("fixed",)

_HEX_OR_LONG_DIGITS = re.compile(r"\b[0-9a-f]{8,}\b|\b\d{4,}\b")
_WS = re.compile(r"\s+")


def normalise_location(location: str) -> str:
    """Reduce a reported location to something two reports can agree on.

    A reviewer may write `POST /api/billing/checkout`, `/api/billing/checkout/`, or
    `api/billing/checkout?plan=pro` for one endpoint. Lowercasing, dropping the
    HTTP verb and query string, collapsing whitespace and stripping slashes makes
    those one string. Long digit and hex runs go too, so `/orders/48291` and
    `/orders/50133` are the same location -- an id in a path is an example, not a
    distinguishing feature.
    """
    text = (location or "").strip().lower()
    text = re.sub(r"^(get|post|put|patch|delete|head|options)\s+", "", text)
    text = text.split("?", 1)[0]
    text = _HEX_OR_LONG_DIGITS.sub("", text)
    text = _WS.sub(" ", text).strip().strip("/")
    return text


def fingerprint_for(*, product_id: str, kind: str, location: str, title: str) -> str:
    """A stable identity for one defect.

    Location is the discriminator when present -- it is the closest thing to an
    objective fact about where a defect lives. Without one, the normalised title is
    the fallback, which is weaker (wording drifts) and is exactly why
    `find_matching_issue` also does a similarity pass before trusting a miss.
    """
    basis = normalise_location(location) or _WS.sub(" ", (title or "").strip().lower())
    raw = f"{product_id}|{kind}|{basis}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def looks_like_same_issue(a_title: str, a_location: str, b_title: str, b_location: str) -> bool:
    """Whether two findings describe one defect, by wording rather than identity.

    The second line of defence, for when the fingerprint misses because the
    reviewer named the location differently ("the subscribe button" vs
    "#subscribe-btn"). Compares title and location together: a title alone is too
    thin a signal, since "the button does not work" describes many buttons.
    """
    left = tokenize(f"{a_title} {a_location}", include_numeric=True)
    right = tokenize(f"{b_title} {b_location}", include_numeric=True)
    if not left or not right:
        return False
    return jaccard(left, right) >= SAME_ISSUE_THRESHOLD


def is_actionable(kind: str) -> bool:
    """Whether finding this should be allowed to commission repair work by itself.

    A feature request is a real thing to record and a wrong thing to hand a repair
    agent unprompted -- it has no "expected behaviour" to restore, so the agent
    would invent a specification and then build against it.
    """
    return kind in ACTIONABLE_KINDS
