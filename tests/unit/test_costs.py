"""Cost accounting.

Anthropic's ``usage.input_tokens`` is *exclusive* of cached tokens; Gemini's raw
``prompt_token_count`` is *inclusive* of them. `estimate_usd` assumes the Anthropic
(exclusive) convention unconditionally — normalizing Gemini onto that convention is
`GeminiProvider`'s job (see `test_gemini_provider.py`), not this module's. Before this
fix, `estimate_usd` re-subtracted `cache_read_tokens` from an already-exclusive
`input_tokens`, so any Anthropic call that hit the cache reported a cost close to
zero, and `cache_write_tokens` (billed at a 1.25x premium) were never priced at all.
"""

from __future__ import annotations

from factory.observability.costs import _DEFAULT_PRICE, PRICE_TABLE, estimate_usd


def test_plain_call_is_priced_at_the_full_input_and_output_rate() -> None:
    price_in, price_out = PRICE_TABLE["claude-opus-5"]
    cost = estimate_usd("claude-opus-5", input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == round(price_in + price_out, 6)


def test_cache_reads_are_billed_at_a_tenth_of_input_price_not_subtracted_from_it() -> None:
    """Regression: the old formula computed `max(input_tokens - cache_read, 0)`, which
    on Anthropic's exclusive `input_tokens` zeroed out real billable input the moment
    caching worked. `input_tokens` and `cache_read_tokens` are billed independently."""
    price_in, _ = PRICE_TABLE["claude-opus-5"]
    cost = estimate_usd(
        "claude-opus-5", input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000
    )
    assert cost == round(price_in + price_in * 0.1, 6)


def test_cache_writes_are_billed_at_a_1_25x_premium() -> None:
    """Cache writes were previously not priced at all."""
    price_in, _ = PRICE_TABLE["claude-opus-5"]
    cost = estimate_usd(
        "claude-opus-5", input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000
    )
    assert cost == round(price_in * 1.25, 6)


def test_an_unrecognised_model_falls_back_to_the_default_price() -> None:
    cost = estimate_usd("some-future-model-id", input_tokens=1_000_000, output_tokens=0)
    assert cost == round(_DEFAULT_PRICE[0], 6)


def test_zero_usage_costs_nothing() -> None:
    assert estimate_usd("claude-opus-5", input_tokens=0, output_tokens=0) == 0.0
