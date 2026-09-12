"""`DesignBrief`'s schema bounds -- pure Pydantic validation, no DB, no LLM.
See `tests/integration/test_design_brief.py` for `build_design_brief` itself
(DB-dependent: the cache lives on a real `Product` row).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factory.engines.development.design_brief import DesignBrief


def _brief(**overrides: object) -> DesignBrief:
    defaults: dict[str, object] = {
        "palette": ["#0B0E14", "#E2A33D"],
        "typography": "A bold sans-serif headline over a system-UI body.",
        "tone": "Minimal and technical, aimed at an engineering audience.",
    }
    defaults.update(overrides)
    return DesignBrief.model_validate(defaults)


def test_a_well_formed_brief_validates() -> None:
    brief = _brief()
    assert brief.palette == ["#0B0E14", "#E2A33D"]


def test_palette_must_have_at_least_two_colors() -> None:
    with pytest.raises(ValidationError):
        _brief(palette=["#0B0E14"])


def test_palette_caps_at_six_colors() -> None:
    with pytest.raises(ValidationError):
        _brief(palette=[f"#{i:06x}" for i in range(7)])


def test_typography_has_a_minimum_length() -> None:
    with pytest.raises(ValidationError):
        _brief(typography="short")


def test_tone_has_a_minimum_length() -> None:
    with pytest.raises(ValidationError):
        _brief(tone="short")


def test_an_unexpected_field_is_rejected() -> None:
    """`Contract`'s `extra="forbid"` -- a model that returns a field this
    schema never asked for must fail loudly, not be silently dropped."""
    with pytest.raises(ValidationError):
        _brief(unexpected_field="surprise")
