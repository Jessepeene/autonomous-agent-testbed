"""The live AB-test decision.

Reuses `learning/stats.py::fisher_exact_less` (the same hypergeometric test the
Learning Loop's sequential trials already use) rather than a new statistic --
these tests cover the decision logic wrapped around it, not the math itself
(already covered where `fisher_exact_less` is defined/tested).
"""

from __future__ import annotations

from factory.engines.development.ab_testing import evaluate_ab_test


def test_a_clear_winner_is_declared_when_significant() -> None:
    # B converts at 40%, A at 4% -- an enormous, obviously real gap.
    verdict = evaluate_ab_test(
        a_views=200, a_signals=8, b_views=200, b_signals=80, min_views_per_arm=30, alpha=0.05
    )
    assert verdict == "b"


def test_the_reverse_gap_declares_the_other_winner() -> None:
    verdict = evaluate_ab_test(
        a_views=200, a_signals=80, b_views=200, b_signals=8, min_views_per_arm=30, alpha=0.05
    )
    assert verdict == "a"


def test_a_near_identical_rate_is_inconclusive() -> None:
    verdict = evaluate_ab_test(
        a_views=200, a_signals=20, b_views=200, b_signals=22, min_views_per_arm=30, alpha=0.05
    )
    assert verdict == "inconclusive"


def test_below_the_minimum_sample_is_inconclusive_even_with_a_huge_apparent_gap() -> None:
    """5 views is not a rate yet, regardless of how lopsided the raw numbers look --
    same reasoning as learning/trials.py's _MIN_ABSOLUTE_N floor."""
    verdict = evaluate_ab_test(
        a_views=5, a_signals=0, b_views=5, b_signals=5, min_views_per_arm=30, alpha=0.05
    )
    assert verdict == "inconclusive"


def test_only_one_arm_below_minimum_is_still_inconclusive() -> None:
    verdict = evaluate_ab_test(
        a_views=200, a_signals=80, b_views=10, b_signals=1, min_views_per_arm=30, alpha=0.05
    )
    assert verdict == "inconclusive"


def test_zero_signals_on_both_sides_does_not_crash_or_falsely_declare_a_winner() -> None:
    """A degenerate all-zero case is exactly the edge learning/trials.py's own
    _MIN_ABSOLUTE_N comment warns about -- fisher_exact_less(0,0,...) can return
    1.0 for every comparison, which must not be misread as a significant result."""
    verdict = evaluate_ab_test(
        a_views=50, a_signals=0, b_views=50, b_signals=0, min_views_per_arm=30, alpha=0.05
    )
    assert verdict == "inconclusive"


def test_the_result_is_symmetric_regardless_of_which_arm_is_which() -> None:
    """Swapping which variant is called "a" and which is "b" must swap the
    verdict, not change whether a winner is found at all."""
    forward = evaluate_ab_test(
        a_views=200, a_signals=8, b_views=200, b_signals=80, min_views_per_arm=30, alpha=0.05
    )
    swapped = evaluate_ab_test(
        a_views=200, a_signals=80, b_views=200, b_signals=8, min_views_per_arm=30, alpha=0.05
    )
    assert {forward, swapped} == {"a", "b"}
    assert forward != swapped


def test_a_tighter_alpha_makes_the_same_data_harder_to_call() -> None:
    """A borderline gap that clears a lenient bar should not necessarily clear a
    stricter one."""
    kwargs = dict(a_views=100, a_signals=10, b_views=100, b_signals=22, min_views_per_arm=30)
    lenient = evaluate_ab_test(**kwargs, alpha=0.10)
    strict = evaluate_ab_test(**kwargs, alpha=0.001)
    assert lenient != "inconclusive"
    assert strict == "inconclusive"
