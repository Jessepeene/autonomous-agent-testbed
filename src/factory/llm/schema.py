"""JSON Schema emission for structured outputs.

Both providers accept a JSON Schema but neither accepts the whole dialect. Pydantic
emits validation keywords (``minLength``, ``maximum``, ``pattern`` …) that the
structured-output engines reject or ignore, so they are stripped before the request.

Nothing is lost by stripping them: the response is still validated against the full
Pydantic model on our side, so a violated bound raises ``SchemaViolation`` rather
than silently passing.
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
    return schema
