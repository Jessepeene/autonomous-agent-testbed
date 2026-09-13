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


# --- the price table cannot silently miss a model we actually call --------------


def test_every_model_the_router_can_choose_is_priced() -> None:
    """Three times now, a model the factory really calls has been missing from
    PRICE_TABLE and silently billed at Claude Sonnet's rate:

      1. gemini-pro-latest / gemini-flash-latest -- production uses Google's rolling
         aliases, never the dated names the table held.
      2. deepseek-v4-pro -- never added when CODE_REPAIR was routed to it.
      3. deepseek-flash -- never added when POC_MODEL_OVERRIDE was introduced.

    The third one inverted every decision made from the number: a repair agent cut
    off by a cost cap it had not actually reached, a spend breaker reading spend
    that never happened, and a cheap-model POC whose entire cost rationale was
    invisible in its own metrics -- a 13x overstatement.

    A comment warning about it was already in the file and did not prevent the third
    occurrence, so this is the guard instead."""
    from factory.llm.router import model_for
    from factory.observability.costs import PRICE_TABLE
    from factory.settings import Settings, TaskType

    for label, settings in (
        ("default routing", Settings()),
        ("POC override", Settings(poc_model_override="deepseek-flash")),
    ):
        for task_type in TaskType:
            model = model_for(task_type, settings)
            assert model in PRICE_TABLE, (
                f"{label}: {task_type.value} routes to {model!r}, which is not in "
                "PRICE_TABLE and would be billed at Claude Sonnet's rate"
            )


def test_deepseek_is_priced_below_claude() -> None:
    """The whole reason DeepSeek is used. If its entry ever drifts above Claude's,
    the cheap path is no longer cheap and someone should find out from a failing
    test rather than from a bill."""
    from factory.observability.costs import PRICE_TABLE

    claude_in, claude_out = PRICE_TABLE["claude-sonnet-5"]
    for model in ("deepseek-flash", "deepseek-v4-pro"):
        price_in, price_out = PRICE_TABLE[model]
        assert price_in < claude_in
        assert price_out < claude_out
