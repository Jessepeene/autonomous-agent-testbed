"""Fisher's exact test — the statistic the trial evaluator's rollback decision rests
on. These are the plan's worked numeric examples as literal assertions, so a threshold
or formula regression is caught here, not discovered later against real trial data."""

from __future__ import annotations

import pytest

from factory.learning.stats import fisher_exact_less


def test_a_clear_regression_is_significant_at_alpha_05() -> None:
    # Trial 6/40 (15.0%) vs. baseline 40/120 (33.3%).
    assert fisher_exact_less(6, 34, 40, 80) == pytest.approx(0.0188, abs=1e-4)


def test_a_moderate_regression_is_not_significant_at_this_sample_size() -> None:
    # Trial 8/40 (20.0%) vs. baseline 40/120 (33.3%) — real but unprovable at n=40.
    assert fisher_exact_less(8, 32, 40, 80) == pytest.approx(0.0792, abs=1e-4)


def test_a_clear_improvement_is_nowhere_near_significant() -> None:
    # Trial 14/40 (35.0%) vs. baseline 40/120 (33.3%).
    assert fisher_exact_less(14, 26, 40, 80) == pytest.approx(0.6534, abs=1e-4)


def test_the_outreach_power_problem_is_real_not_hand_waved() -> None:
    """At a realistic ~1.5% baseline, a 200-send trial with zero conversions still
    doesn't clear significance — this is why conversion rate must never be the sole
    gating metric for the outreach prompt (see allowlist.DEFAULT_METRIC)."""
    p = fisher_exact_less(0, 200, 6, 394)
    assert p == pytest.approx(0.0867, abs=1e-4)
    assert p > 0.05


def test_identical_rates_are_not_significant() -> None:
    """Not 1.0 — this is P(X <= observed), and the observed count sits at the
    distribution's centre when the true rates match, not at an extreme tail."""
    assert fisher_exact_less(10, 90, 10, 90) > 0.3


def test_symmetry_of_the_null_case() -> None:
    """A trial that outperforms the baseline should never register as a 'lower
    rate' regression, regardless of magnitude."""
    assert fisher_exact_less(90, 10, 10, 90) == pytest.approx(1.0, abs=1e-9)
