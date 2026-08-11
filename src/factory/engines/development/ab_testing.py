"""The live AB-test decision: is one variant's signal rate significantly better?

Deliberately not `learning/trials.py`'s machinery: that loop compares one prompt
version's outcomes against a *different time period*'s historical episodes for a
different prompt version -- sequential, one arm live at a time. This compares two
arms served *concurrently*, over the *same* window, to each other directly.

The underlying statistic is identical either way, though -- a one-sided Fisher's
exact test on a 2x2 table of (successes, failures) between two groups doesn't care
whether those groups are "trial vs. history" or "variant A vs. variant B". Reuses
`learning/stats.py::fisher_exact_less` rather than duplicating the hypergeometric
math.
"""

from __future__ import annotations

from typing import Literal

from factory.learning.stats import fisher_exact_less

Verdict = Literal["a", "b", "inconclusive"]


def evaluate_ab_test(
    *,
    a_views: int,
    a_signals: int,
    b_views: int,
    b_signals: int,
    min_views_per_arm: int,
    alpha: float,
) -> Verdict:
    """Which variant wins, or "inconclusive" if the data doesn't yet say.

    Below ``min_views_per_arm`` on either side, a rate is not a rate yet --
    returns "inconclusive" without even running the test, the same reasoning as
    `learning/trials.py`'s `_MIN_ABSOLUTE_N` floor (a trial with near-zero
    episodes would otherwise let the test's own edge cases -- comparing 0 against
    0 -- produce a spurious verdict).
    """
    if a_views < min_views_per_arm or b_views < min_views_per_arm:
        return "inconclusive"

    a_fail, b_fail = a_views - a_signals, b_views - b_signals
    # One-sided each direction: is A's rate significantly lower than B's, or B's
    # lower than A's? A small p-value on one side is what promotes the *other*
    # side to winner -- symmetric, so which arm happens to be "group 1" in the
    # call never biases the result.
    p_a_lower = fisher_exact_less(a_signals, a_fail, b_signals, b_fail)
    p_b_lower = fisher_exact_less(b_signals, b_fail, a_signals, a_fail)
    if p_a_lower < alpha:
        return "b"
    if p_b_lower < alpha:
        return "a"
    return "inconclusive"
