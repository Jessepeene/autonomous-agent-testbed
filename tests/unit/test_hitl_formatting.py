"""`hitl/formatting.py::build_escalation_text` -- the one place an escalation's
Telegram text is assembled, used by both the initial push
(`interrupts.py::deliver_pending`) and the `/issue` drill-down
(`commands.py::build_issue_detail`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from factory.hitl import formatting
from factory.state.orm import Interrupt, Product


def _interrupt(**overrides: object) -> Interrupt:
    base: dict[str, object] = {
        "id": "a1b2c3d4e5f6",
        "reason": "heal_exhausted",
        "subject": "Some product needs help",
        "detail": "Automated repair did not converge.",
        "headline": "\U0001f527 Repair stuck — needs your input",
        "reply_hint": "Reply with guidance for one more attempt, or REJECT to retire this product.",
        "needs_reply": True,
        "task_id": None,
        "product_id": None,
        "status": "open",
        "delivered": False,
        "response": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "resolved_at": None,
    }
    base.update(overrides)
    return Interrupt(**base)


def _product(**overrides: object) -> Product:
    base: dict[str, object] = {
        "id": "prod-1",
        "name": "Freelance bookkeepers on QuickBooks or Xero",
        "idea_id": "idea-1",
        "status": "validating",
    }
    base.update(overrides)
    return Product(**base)


async def test_product_name_is_the_first_line_when_product_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product = _product()

    async def fake_get_product(session: object, product_id: str) -> Product:
        assert product_id == "prod-1"
        return product

    monkeypatch.setattr(formatting.repository, "get_product", fake_get_product)
    interrupt = _interrupt(product_id="prod-1")

    text = await formatting.build_escalation_text(None, interrupt)  # type: ignore[arg-type]

    lines = text.splitlines()
    assert lines[0] == f"\U0001f4e6 {product.name}"
    assert lines[1] == interrupt.headline


async def test_no_product_line_when_not_product_scoped() -> None:
    interrupt = _interrupt(product_id=None)

    text = await formatting.build_escalation_text(None, interrupt)  # type: ignore[arg-type]

    assert "\U0001f4e6" not in text
    assert text.splitlines()[0] == interrupt.headline


async def test_sections_are_labelled_and_separated() -> None:
    interrupt = _interrupt(product_id=None)

    text = await formatting.build_escalation_text(None, interrupt)  # type: ignore[arg-type]

    assert "\U0001f50d The problem:" in text
    assert interrupt.detail in text
    assert "\U0001f449 What to do:" in text
    assert interrupt.reply_hint in text
    # The problem section precedes the what-to-do section.
    assert text.index("The problem:") < text.index("What to do:")


async def test_needs_reply_true_asks_to_include_the_tag() -> None:
    interrupt = _interrupt(product_id=None, needs_reply=True)

    text = await formatting.build_escalation_text(None, interrupt)  # type: ignore[arg-type]

    assert f"Include this tag in your reply: #{formatting.tag_for(interrupt.id)}" in text
    assert f"Tag: #{formatting.tag_for(interrupt.id)}" not in text


async def test_needs_reply_false_shows_a_reference_only_tag() -> None:
    interrupt = _interrupt(product_id=None, needs_reply=False)

    text = await formatting.build_escalation_text(None, interrupt)  # type: ignore[arg-type]

    assert f"Tag: #{formatting.tag_for(interrupt.id)}" in text
    assert "Include this tag in your reply" not in text


async def test_falls_back_to_the_raw_reason_when_headline_is_unset() -> None:
    """Historical-row safety: every live raise site sets `headline`, but a row
    from before this migration (or any future gap) must not crash."""
    interrupt = _interrupt(product_id=None, headline=None)

    text = await formatting.build_escalation_text(None, interrupt)  # type: ignore[arg-type]

    assert "[HEAL_EXHAUSTED]" in text


async def test_falls_back_to_a_generic_reply_hint_when_unset() -> None:
    interrupt = _interrupt(product_id=None, reply_hint=None)

    text = await formatting.build_escalation_text(None, interrupt)  # type: ignore[arg-type]

    assert "Reply with your answer." in text


def test_tag_for_is_the_first_eight_characters() -> None:
    assert formatting.tag_for("a1b2c3d4e5f6") == "a1b2c3d4"
