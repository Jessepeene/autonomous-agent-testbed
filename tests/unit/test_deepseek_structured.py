"""Salvaging the one shape failure DeepSeek actually causes, and no others.

DeepSeek's Anthropic-compatible endpoint does not enforce
`additionalProperties: false` during tool-call decoding -- the schema says it, the
endpoint treats it as advice. Live consequence: a Bug Council reviewer attached an
empty `line_placeholder` to every finding, `Contract`'s `extra="forbid"` rejected
the entire response, and a patch with nothing wrong with it was escalated to a
human as a failed code review.

`extra="forbid"` is worth keeping -- a field the schema never defined usually means
the model misunderstood the task. So exactly one rescue is allowed, and these tests
draw that line: stray keys are dropped, everything else still fails.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factory.llm.providers.deepseek_provider import _validate_tolerating_extras
from factory.state.schemas import Contract


class _Finding(Contract):
    file: str
    line: int | None = None


class _Report(Contract):
    summary: str
    findings: list[_Finding]


def test_a_clean_payload_is_unchanged() -> None:
    value = _validate_tolerating_extras(
        _Report, {"summary": "ok", "findings": [{"file": "app/main.py", "line": 4}]}
    )
    assert value.summary == "ok"
    assert value.findings[0].line == 4


def test_the_live_failure_is_salvaged() -> None:
    """The exact shape that escalated a good patch: an extra empty field on each
    item of a nested list."""
    value = _validate_tolerating_extras(
        _Report,
        {
            "summary": "two findings",
            "findings": [
                {"file": "app/main.py", "line": 9, "line_placeholder": ""},
                {"file": "app/api.py", "line": 2, "line_placeholder": ""},
            ],
        },
    )
    assert [f.file for f in value.findings] == ["app/main.py", "app/api.py"]


def test_a_stray_key_at_the_top_level_is_salvaged() -> None:
    value = _validate_tolerating_extras(
        _Report, {"summary": "ok", "findings": [], "confidence": 0.9}
    )
    assert value.summary == "ok"


def test_a_missing_required_field_still_fails() -> None:
    """The rescue must not become "accept anything". A missing field means the model
    got the task wrong, not the shape."""
    with pytest.raises(ValidationError):
        _validate_tolerating_extras(_Report, {"findings": []})


def test_a_wrongly_typed_field_still_fails() -> None:
    with pytest.raises(ValidationError):
        _validate_tolerating_extras(
            _Report, {"summary": "ok", "findings": [{"file": "x", "line": "not a number"}]}
        )


def test_a_stray_key_alongside_a_real_error_still_fails() -> None:
    """Mixed errors must not be partially salvaged -- if anything beyond an extra
    key is wrong, the whole response is suspect and the caller should see the
    SchemaViolation."""
    with pytest.raises(ValidationError):
        _validate_tolerating_extras(
            _Report, {"findings": [{"file": "x", "line_placeholder": ""}], "extra": 1}
        )
