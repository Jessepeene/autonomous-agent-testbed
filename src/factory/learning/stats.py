"""The trial-evaluation statistic.

One-sided Fisher's exact test, computed directly from the hypergeometric distribution
via ``math.comb`` — no ``scipy``/``numpy`` dependency (neither is in ``pyproject.toml``).
Chosen over a two-proportion z-test because this system's real sample sizes (tens to
low hundreds per week) are too small for the z-test's normal approximation to be
trustworthy; Fisher's exact needs no approximation and no continuity correction.
"""

from __future__ import annotations

from math import comb


def fisher_exact_less(a: int, b: int, c: int, d: int) -> float:
    """One-sided p-value for "group 1's success rate is lower than group 2's".

    Group 1: ``a`` successes, ``b`` failures. Group 2: ``c`` successes, ``d`` failures.
    Returns the probability, under the null hypothesis that the two groups share the
    same true rate, of observing a group-1 success count at or below ``a`` — the exact
    hypergeometric tail sum, not an approximation.
    """
    n1, n2 = a + b, c + d
    successes, total = a + c, a + b + c + d
    lo = max(0, successes - n2)

    def p_exact(x: int) -> float:
        return comb(n1, x) * comb(n2, successes - x) / comb(total, successes)

    return sum(p_exact(x) for x in range(lo, a + 1))
