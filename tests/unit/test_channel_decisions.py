"""CHANNEL_SCOUT's human-approval decision parsing -- the pure, DB-free piece.

`sync_decisions` itself reads and writes `channel_experiments`, which is Postgres-
specific and covered by an integration test instead (see
`tests/integration/test_growth_roles_integration.py`). What is tested here is the
fail-closed parser alone -- mirrors `test_campaign_approval.py`'s identical split,
since `channel._parse_decision` is deliberately the same contract: only an explicit,
unambiguous "approve" ever authorises anything.
"""

from __future__ import annotations

import pytest

from factory.engines.marketing.channel import _parse_decision


@pytest.mark.parametrize("reply", ["APPROVE", "approve", "Approve, sounds good", "  approve  "])
def test_unambiguous_approve_is_approved(reply: str) -> None:
    assert _parse_decision(reply) == "approved"


@pytest.mark.parametrize(
    "reply",
    [
        "REJECT",
        "reject",
        "no",
        "not sure about this one",
        "acknowledged",
        "",
        "why this subreddit specifically?",
        "disapprove",
    ],
)
def test_anything_else_is_rejected(reply: str) -> None:
    assert _parse_decision(reply) == "rejected"
