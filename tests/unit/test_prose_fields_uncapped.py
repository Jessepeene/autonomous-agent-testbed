"""No `max_length` on a prose string an LLM writes.

`Contract`'s docstring has said this since the third time it cost real work. It
kept costing real work anyway, because a docstring is a thing you have to
remember at the moment you add a field, and the schemas added since ignored it:

- `ReviewVerdict.summary` at 200 discarded a genuine review of a genuine defect.
- `ReviewVerdict.details` at 2000 discarded the retry of it.
- `RequirementGap.evidence` at 600 discarded a gap report, live, on 2026-09-14.
- `ReleaseDecision.rationale` at 1500 discarded a fully-reasoned release
  decision, live, the same morning -- the highest-judgement call in the company,
  thrown away for being 1,501 characters long.

So the rule stops being a docstring and becomes a test. The failure mode is
always the same and always total: a verbose but *correct* answer becomes a
validation error, the call is retried or abandoned, and the work is lost. The
model is never told the limit exists, so it cannot avoid it.

These caps bound nothing real either. The columns behind them are `Text`, every
consumer truncates at the point of use, and the actual ceiling on a response is
`max_tokens`, enforced by the API rather than by hope.

Two things are deliberately still allowed:

- **`min_length`**, which is the opposite: an empty rationale really is useless,
  and failing on it catches a defect instead of creating one.
- **`max_length` on a list**, which bounds how many items come back. A report
  with five hundred findings is broken in a way a long sentence is not.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic.fields import FieldInfo

from factory.state import schemas
from factory.state.schemas import Contract
from factory.tools.product_review import ReviewVerdict

#: Short strings that are identifiers or enum-ish tokens rather than prose. A cap
#: here is a real constraint on shape -- `ref` is a footnote marker like "[3]",
#: and a model writing 400 characters into it has misunderstood the field, not
#: been unusually thorough.
STRUCTURAL_FIELDS = {
    ("LeadAssessment", "ref"),
    # A capability or department name, checked against a closed allowlist the model
    # is shown (`learning/feedback.py::ALLOWED_CAPABILITIES`). A cap is a real
    # constraint on shape here: nothing this company has is 60 characters long, and a
    # model writing a sentence into it has misread the field rather than been
    # thorough -- and the answer is dropped by the allowlist either way.
    ("ProposedLesson", "capability"),
}


def _contract_models() -> list[type[Contract]]:
    found = {
        obj
        for _, obj in inspect.getmembers(schemas, inspect.isclass)
        if issubclass(obj, Contract) and obj is not Contract
    }
    # Lives in tools/, not schemas.py, and is the field that started this.
    found.add(ReviewVerdict)
    return sorted(found, key=lambda m: m.__name__)


def _prose_fields_with_caps(model: type[Contract]) -> list[tuple[str, FieldInfo]]:
    capped = []
    for name, field in model.model_fields.items():
        if (model.__name__, name) in STRUCTURAL_FIELDS:
            continue
        # Only bare `str`. A `list[str]` cap bounds item count, which is legitimate.
        if field.annotation is not str:
            continue
        for meta in field.metadata:
            if getattr(meta, "max_length", None) is not None:
                capped.append((name, field))
    return capped


@pytest.mark.parametrize("model", _contract_models(), ids=lambda m: m.__name__)
def test_no_prose_field_an_llm_writes_carries_a_max_length(model: type[Contract]) -> None:
    capped = _prose_fields_with_caps(model)
    assert not capped, (
        f"{model.__name__} caps prose an LLM writes: "
        f"{', '.join(name for name, _ in capped)}. Every one of these limits has "
        "been wrong when it fired, and firing means the whole response is "
        "discarded -- the model was never told the limit existed. The real ceiling "
        "is max_tokens. Remove the max_length; keep min_length if emptiness is a "
        "defect. If this is an identifier rather than prose, add it to "
        "STRUCTURAL_FIELDS with a reason."
    )


def test_the_two_fields_that_failed_in_production_are_covered() -> None:
    """Guards the guard: these are the exact fields that lost work on 2026-09-14."""
    from factory.state.schemas import ReleaseDecision, RequirementGap

    assert not _prose_fields_with_caps(ReleaseDecision)
    assert not _prose_fields_with_caps(RequirementGap)
    # And the constraint that must survive: emptiness is still a failure.
    assert ReleaseDecision.model_fields["rationale"].is_required()


def test_list_caps_are_still_allowed() -> None:
    """A cap on how many findings come back is not the thing this forbids."""
    from factory.state.schemas import IdeaBatch

    field = IdeaBatch.model_fields["ideas"]
    assert any(getattr(m, "max_length", None) == 25 for m in field.metadata), (
        "list caps bound cost and must keep working; only bare-str prose caps are "
        "forbidden"
    )
