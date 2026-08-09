"""Money invariants: the 50% split, integer cents, and never going negative."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from factory.engines.finance.reinvest import (
    REINVESTMENT_BASIS_POINTS,
    claw_back,
    split_revenue,
)
from factory.engines.finance.webhooks import (
    REFUND_EVENTS,
    REVENUE_EVENTS,
    _amount_cents,
    _product_id,
)


def test_split_is_exactly_half_on_even_amounts() -> None:
    split = split_revenue(10_000)
    assert split.reinvest_cents == 5_000
    assert split.retained_cents == 5_000


def test_odd_cent_goes_to_retained_not_the_spendable_pool() -> None:
    """Rounding must never invent a cent the factory can spend."""
    split = split_revenue(101)
    assert split.reinvest_cents == 50
    assert split.retained_cents == 51


@given(amount=st.integers(min_value=0, max_value=10_000_000))
def test_split_conserves_every_cent(amount: int) -> None:
    split = split_revenue(amount)
    assert split.total_cents == amount
    assert split.reinvest_cents >= 0
    assert split.retained_cents >= 0


@given(amount=st.integers(min_value=0, max_value=10_000_000))
def test_reinvestment_never_exceeds_half(amount: int) -> None:
    split = split_revenue(amount)
    assert split.reinvest_cents * 2 <= amount


def test_split_rejects_negative_amounts() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        split_revenue(-1)


def test_basis_points_are_exactly_fifty_percent() -> None:
    assert REINVESTMENT_BASIS_POINTS == 5_000


# --- refunds ----------------------------------------------------------------


def test_claw_back_takes_half_when_the_pool_can_cover_it() -> None:
    split = claw_back(1_000, available_pool_cents=5_000)
    assert split.reinvest_cents == -500
    assert split.retained_cents == -500


def test_claw_back_clamps_to_an_empty_pool() -> None:
    """A refund arriving after the reinvested half was spent must not go negative."""
    split = claw_back(1_000, available_pool_cents=0)
    assert split.reinvest_cents == 0
    assert split.retained_cents == -1_000


def test_claw_back_clamps_to_a_partially_spent_pool() -> None:
    split = claw_back(1_000, available_pool_cents=200)
    assert split.reinvest_cents == -200
    assert split.retained_cents == -800


@given(
    refund=st.integers(min_value=0, max_value=1_000_000),
    pool=st.integers(min_value=0, max_value=1_000_000),
)
def test_claw_back_never_drains_more_than_the_pool_holds(refund: int, pool: int) -> None:
    split = claw_back(refund, pool)
    assert -split.reinvest_cents <= pool
    assert split.total_cents == -refund


# --- webhook amount extraction ---------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount_total", 2_500),
        ("amount_paid", 1_999),
        ("amount_received", 700),
        ("amount_refunded", 300),
        ("amount", 100),
    ],
)
def test_amount_is_read_from_each_stripe_shape(field: str, value: int) -> None:
    event = {"id": "evt_1", "type": "invoice.paid", "data": {"object": {field: value}}}
    assert _amount_cents(event) == value


def test_unknown_event_shape_yields_zero() -> None:
    assert _amount_cents({"id": "evt", "type": "x", "data": {"object": {}}}) == 0


def test_revenue_and_refund_event_sets_are_disjoint() -> None:
    assert not (REVENUE_EVENTS & REFUND_EVENTS)


# --- webhook product_id extraction ------------------------------------------


def test_product_id_is_read_from_client_reference_id() -> None:
    event = {"data": {"object": {"client_reference_id": "prod-123"}}}
    assert _product_id(event) == "prod-123"


def test_product_id_falls_back_to_metadata() -> None:
    event = {"data": {"object": {"metadata": {"product_id": "prod-456"}}}}
    assert _product_id(event) == "prod-456"


def test_client_reference_id_wins_over_metadata_when_both_present() -> None:
    event = {
        "data": {
            "object": {
                "client_reference_id": "prod-123",
                "metadata": {"product_id": "prod-456"},
            }
        }
    }
    assert _product_id(event) == "prod-123"


def test_product_id_is_none_when_neither_field_is_present() -> None:
    """Routine, not an error: a product deployed before this contract existed, or an
    event type carrying neither field, must never block revenue from being recorded."""
    assert _product_id({"data": {"object": {}}}) is None


def test_product_id_ignores_an_empty_string_client_reference_id() -> None:
    event = {"data": {"object": {"client_reference_id": ""}}}
    assert _product_id(event) is None
