"""Gemini usage normalization.

Gemini's `prompt_token_count` is *inclusive* of cached tokens; Anthropic's
`input_tokens` is exclusive. `estimate_usd` (see `test_costs.py`) assumes the
exclusive convention unconditionally, so `GeminiProvider` must normalize at the one
place that knows which convention this vendor speaks, rather than costs.py trying to
guess which vendor produced a given row.
"""

from __future__ import annotations

from types import SimpleNamespace

from factory.llm.providers.gemini_provider import _billable_input_tokens


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
