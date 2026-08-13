"""The top-level legal gate on outbound campaigns — the pure, DB-free pieces.

`ensure_approval`/`sync_decisions` themselves read and write `campaign_approvals` via
`ON CONFLICT DO NOTHING`, which is Postgres-specific and therefore covered by an
integration test rather than here (see `tests/integration/test_campaign_approval.py`).
What is tested here is the fail-closed decision parser and the brief's PII discipline
— both pure functions, and both load-bearing: a parsing bug here is the difference
between a stray Telegram reply blocking a campaign forever and one silently
authorising it.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from factory.engines.marketing.campaign import RECHECK_SECONDS, _build_brief, _parse_decision
from factory.state.orm import Lead


def _lead(**overrides: object) -> Lead:
    base: dict[str, object] = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "job_title": "Head of Finance",
        "company": "Analytical Engines BV",
        "work_email": "ada@analytical-engines.test",
        "source_url": "https://analytical-engines.test/team",
        "scraped_at": datetime(2026, 1, 1, tzinfo=UTC),
        "provenance_kind": "primary_source",
        "provider": "hunter.io",
    }
    base.update(overrides)
    return Lead(**base)


# --- fail-closed decision parsing ---------------------------------------------


@pytest.mark.parametrize(
    "reply",
    ["APPROVE", "approve", "Approve, go ahead", "  approve  "],
)
def test_unambiguous_approve_is_approved(reply: str) -> None:
    assert _parse_decision(reply) == "approved"


@pytest.mark.parametrize(
    "reply",
    [
        "REJECT",
        "reject",
        "no",
        "not yet, let me review the leads first",
        "acknowledged",  # the generic interrupt-resolution fallback text
        "",
        "why are these leads not verified?",
        "disapprove",  # contains "appro" but is not an approval
    ],
)
def test_anything_else_is_rejected(reply: str) -> None:
    """Silence, confusion, or an outright rejection must never be read as
    authorisation to open the SMTP transport."""
    assert _parse_decision(reply) == "rejected"


# --- brief PII discipline -----------------------------------------------------


def test_brief_names_role_and_company_but_never_email_or_full_name() -> None:
    leads = [_lead(), _lead(first_name="Bob", last_name="Babbage", work_email="bob@engines.test")]
    brief = _build_brief(
        product_name="Reconciliation Assistant", product_id="prod-1", candidates=leads
    )

    assert "Head of Finance" in brief
    assert "Analytical Engines BV" in brief
    assert "ada@analytical-engines.test" not in brief
    assert "bob@engines.test" not in brief
    assert "Lovelace" not in brief
    assert "Babbage" not in brief


def test_brief_truncates_the_sample_and_counts_the_rest() -> None:
    leads = [_lead(work_email=f"lead{i}@engines.test") for i in range(8)]
    brief = _build_brief(product_name="X", product_id="prod-1", candidates=leads)

    assert "Leads ready to contact: 8" in brief
    assert "3 more" in brief


def test_brief_is_pure_content_with_no_reply_instruction_baked_in() -> None:
    """The reply instruction ("Reply APPROVE...") used to live inside the brief
    text itself; it's now a separate `reply_hint` the call site
    (`campaign.py::ensure_approval`) passes to `interrupts.request` directly
    (see `tests/integration/test_campaign_approval.py` for that assertion) --
    the brief is purely the PII-safe compliance content."""
    brief = _build_brief(product_name="X", product_id="prod-1", candidates=[_lead()])
    assert "APPROVE" not in brief
    assert "REJECT" not in brief


def test_recheck_interval_is_a_sane_positive_backoff() -> None:
    """Cheap enough to poll indefinitely without a tight reschedule loop."""
    assert 0 < RECHECK_SECONDS <= 24 * 3600
