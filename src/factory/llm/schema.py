"""JSON Schema emission for structured outputs.

Both providers accept a JSON Schema but neither accepts the whole dialect. Pydantic
emits validation keywords (``minLength``, ``maximum``, ``pattern`` …) that the
structured-output engines reject or ignore, so they are stripped before the request.

Nothing is lost by stripping them: the response is still validated against the full
Pydantic model on our side, so a violated bound raises ``SchemaViolation`` rather
than silently passing.

**A second, unrelated reason to strip, found 2026-09-26: `model_json_schema()` also
emits `description` -- the class's own docstring, and any field's `Field(description=
...)` -- verbatim into the request.** Confirmed live: `StrategicReviewVerdict`'s and
`ProductNext`'s multi-paragraph docstrings, written for a human reading the source
(complete with phase numbers, other class names, file paths -- `"Phase 3/4/5c"`,
`"ManagerDecision"`, `BUG_LOG.md`) were reaching the model on every call, an entire
unreviewed second prompt nobody auditing `SYSTEM`/`PROMPT` constants would ever look
at, since a docstring does not look like a prompt. `_UNSUPPORTED_KEYS` strips a key
because the provider rejects or ignores it; `description` is stripped for the
opposite reason -- every provider accepts it gladly, which is exactly the danger.
This project holds every worker to the rule that a value must say how it is known;
a schema description says nothing anyone chose to say to the model, and must never
reach it. If a field ever needs to instruct the model, that instruction belongs in
`system`/`prompt`, in text someone deliberately wrote as a prompt and can review as
one -- never in a docstring meant for the next engineer to read the source.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

# Keywords the providers' schema engines do not support for constrained decoding.
_UNSUPPORTED_KEYS = frozenset(
    {
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
        "default",
        "examples",
    }
)

def strip_descriptions(node: Any) -> None:
    """Recursively delete every `description` key, in place -- see the module
    docstring for why. Public and standalone, not folded into `_UNSUPPORTED_KEYS`,
    so a provider with its own, differently-shaped cleaning pass (Gemini's
    `_strip_additional_properties`, which deliberately keeps keys this module
    strips) can still apply this one rule without adopting the rest."""
    if isinstance(node, dict):
        node.pop("description", None)
        for value in node.values():
            strip_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            strip_descriptions(item)


def _clean(node: Any) -> Any:
    if isinstance(node, dict):
        cleaned = {k: _clean(v) for k, v in node.items() if k not in _UNSUPPORTED_KEYS}
        if cleaned.get("type") == "object" and "additionalProperties" not in cleaned:
            cleaned["additionalProperties"] = False
        return cleaned
    if isinstance(node, list):
        return [_clean(item) for item in node]
    return node


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Provider-safe JSON Schema for a contract model."""
    raw = model.model_json_schema()
    schema = _clean(raw)
    assert isinstance(schema, dict)
    schema.pop("title", None)
    strip_descriptions(schema)
    return schema
