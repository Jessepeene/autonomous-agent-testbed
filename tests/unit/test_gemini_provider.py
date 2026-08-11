"""Gemini usage normalization.

Gemini's `prompt_token_count` is *inclusive* of cached tokens; Anthropic's
`input_tokens` is exclusive. `estimate_usd` (see `test_costs.py`) assumes the
exclusive convention unconditionally, so `GeminiProvider` must normalize at the one
place that knows which convention this vendor speaks, rather than costs.py trying to
guess which vendor produced a given row.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel, Field, ValidationError

from factory.llm.providers.base import ProviderRequestRejected
from factory.llm.providers.gemini_provider import (
    GeminiProvider,
    _billable_input_tokens,
    _sanitize_tools_for_gemini,
    _strip_additional_properties,
)
from factory.settings import Settings, TaskType


def test_cached_tokens_are_subtracted_from_the_inclusive_prompt_count() -> None:
    meta = SimpleNamespace(prompt_token_count=1000, cached_content_token_count=400)
    assert _billable_input_tokens(meta) == 600


def test_no_cache_hit_leaves_the_prompt_count_unchanged() -> None:
    meta = SimpleNamespace(prompt_token_count=1000, cached_content_token_count=0)
    assert _billable_input_tokens(meta) == 1000


def test_missing_metadata_is_zero_not_a_crash() -> None:
    assert _billable_input_tokens(None) == 0


def test_never_goes_negative_even_on_an_inconsistent_response() -> None:
    meta = SimpleNamespace(prompt_token_count=100, cached_content_token_count=500)
    assert _billable_input_tokens(meta) == 0


class _Nested(BaseModel):
    tags: dict[str, str]


class _Outer(BaseModel):
    items: list[_Nested]
    extra: dict[str, int]


def test_strip_additional_properties_removes_every_occurrence_including_nested_defs() -> None:
    # Pydantic sets additionalProperties wherever a dict[str, ...]-typed field appears
    # -- both directly on the outer model and inside a $defs sub-schema referenced by
    # a nested model -- and the Gemini Developer API rejects the key at every one of
    # those locations, not just the first one found.
    schema = _Outer.model_json_schema()
    assert "additionalProperties" in schema["$defs"]["_Nested"]["properties"]["tags"]
    assert "additionalProperties" in schema["properties"]["extra"]

    _strip_additional_properties(schema)

    dumped = str(schema)
    assert "additionalProperties" not in dumped
    assert "additional_properties" not in dumped


def test_strip_additional_properties_is_a_no_op_on_a_clean_schema() -> None:
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    _strip_additional_properties(schema)
    assert schema == {"type": "object", "properties": {"name": {"type": "string"}}}


class _ComplexItem(BaseModel):
    """Mirrors the field mix that reproduced the live `maxItems` bug: string-length,
    HttpUrl (`format: uri`), and numeric-range constraints together on one item."""

    text: str
    score: float


class _ListWithMaxItems(BaseModel):
    items: list[_ComplexItem] = Field(max_length=25)


def test_strip_additional_properties_also_removes_max_and_min_items() -> None:
    # Reproduced directly against the live Gemini API: maxItems/minItems on an array
    # whose items schema is a $ref'd nested model triggers a bare
    # `400 INVALID_ARGUMENT: Request contains an invalid argument` once that nested
    # model is complex enough (DiscriminatedIdeas.ideas: list[ScoredIdea], 11 fields,
    # was the real failure). Safe to drop: model_validate_json() re-enforces the same
    # bound locally once the response comes back.
    schema = _ListWithMaxItems.model_json_schema()
    assert schema["properties"]["items"]["maxItems"] == 25

    _strip_additional_properties(schema)

    assert "maxItems" not in schema["properties"]["items"]
    assert "minItems" not in schema["properties"]["items"]


def test_sanitize_tools_for_gemini_strips_additional_properties_from_parameters() -> None:
    tools = [
        {
            "name": "run_query",
            "description": "Run a query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filters": {"type": "object", "additionalProperties": {"type": "string"}}
                },
                "additionalProperties": False,
            },
        }
    ]
    sanitized = _sanitize_tools_for_gemini(tools)
    dumped = str(sanitized)
    assert "additionalProperties" not in dumped
    assert "additional_properties" not in dumped


def test_sanitize_tools_for_gemini_does_not_mutate_the_shared_input_schema() -> None:
    # ToolDescriptor.to_gemini() hands over the *same* dict object also read by
    # to_anthropic() for strict-mode schemas (registry.py) -- corrupting it here
    # would silently break Anthropic's tool payloads on their next read of it.
    shared_schema = {"type": "object", "additionalProperties": False}
    tools = [{"name": "t", "description": "d", "parameters": shared_schema}]

    _sanitize_tools_for_gemini(tools)

    assert shared_schema == {"type": "object", "additionalProperties": False}


def test_sanitize_tools_for_gemini_passes_through_a_tool_with_no_parameters() -> None:
    tools = [{"name": "noop", "description": "does nothing", "parameters": {}}]
    assert _sanitize_tools_for_gemini(tools) == [
        {"name": "noop", "description": "does nothing", "parameters": {}}
    ]


# --- latent crash guard: a contract google's own schema model can't represent ----


class _Echo(BaseModel):
    text: str


async def test_parse_raises_provider_request_rejected_on_a_schema_validation_error() -> None:
    """Regression coverage for a latent bug found in the 2026-08-10 audit:
    google-genai's own request-config models (e.g. `types.Schema`) are
    `extra="forbid"` and lack several JSON Schema keywords Claude's side accepts
    (exclusiveMinimum, multipleOf, uniqueItems, const, oneOf/allOf, discriminator).
    A contract using one of those previously raised the SDK's own internal
    pydantic ValidationError uncaught -- neither `except genai_errors.APIError`
    nor `except (OSError, TimeoutError)` catches it -- surfacing a confusing error
    about google's schema model instead of a clean, provider-neutral, failover-
    eligible one. No current contract in `factory.*` triggers this (verified
    during the audit), so this is exercised here by making the SDK's config
    constructor raise directly, standing in for a future contract that would."""
    provider = GeminiProvider(Settings(gemini_api_key="test-key"))
    provider._client = AsyncMock()  # bypass _get_client()'s real client construction

    validation_error = ValidationError.from_exception_data("Schema", [])
    with patch(
        "factory.llm.providers.gemini_provider.genai_types.GenerateContentConfig",
        side_effect=validation_error,
    ):
        with pytest.raises(ProviderRequestRejected):
            await provider.parse(
                _Echo,
                system="s",
                prompt="p",
                task_type=TaskType.ANALYSIS,
                model="gemini-2.5-flash",
            )
