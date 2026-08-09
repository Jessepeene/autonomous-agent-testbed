"""Revenue splitting.

Exactly 50% of net new revenue is reinvested. All arithmetic is in integer cents —
floats would eventually produce a half-cent that either vanishes or is created from
nothing, and a ledger that does not balance is worse than one that is slightly
conservative.

On an odd amount the extra cent goes to *retained*, never to the spendable pool, so
the factory can never authorise a cent it did not earn.
"""

from __future__ import annotations

from typing import NamedTuple

REINVESTMENT_BASIS_POINTS = 5_000  # 50.00%
_BASIS_TOTAL = 10_000


class RevenueSplit(NamedTuple):
    reinvest_cents: int
    retained_cents: int

    @property
    def total_cents(self) -> int:
        return self.reinvest_cents + self.retained_cents


def split_revenue(amount_cents: int) -> RevenueSplit:
    """Split net revenue into (reinvest, retained).

    Raises on a negative amount: refunds are a separate ledger path, not a negative
    reinvestment.
    """
    if amount_cents < 0:
        raise ValueError("split_revenue expects a non-negative amount; refunds use claw_back()")
    reinvest = (amount_cents * REINVESTMENT_BASIS_POINTS) // _BASIS_TOTAL
    return RevenueSplit(reinvest_cents=reinvest, retained_cents=amount_cents - reinvest)


def claw_back(refund_cents: int, available_pool_cents: int) -> RevenueSplit:
    """Reverse a refund's share of the reinvestment pool without going negative.

    A refund can arrive after its reinvested half has already been spent. Clamping to
    what remains keeps the pool at zero instead of driving it negative; the shortfall
    stays on the retained side where it belongs.
    """
    if refund_cents < 0:
        raise ValueError("refund amount must be non-negative")
    intended = (refund_cents * REINVESTMENT_BASIS_POINTS) // _BASIS_TOTAL
    from_pool = min(intended, max(available_pool_cents, 0))
    return RevenueSplit(reinvest_cents=-from_pool, retained_cents=-(refund_cents - from_pool))
