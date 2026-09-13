"""Spend accounting.

Per-provider, per-engine token costs, read from the ``llm_calls`` table. ``cost_usd``
is computed once per call, at write time (``LLMClient``'s usage recorder, via
``estimate_usd`` below), and stored — never recomputed here at read time. If it were
recomputed here, editing ``PRICE_TABLE`` after a price change would retroactively
rewrite last month's spend history to a price that was never actually charged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.state.orm import LLMCall

log = get_logger(__name__)

#: USD per million tokens, (input, output). Update alongside provider pricing.
#:
#: The gemini-*-latest and deepseek-v4-pro keys are not redundant with the dated
#: names above them -- they are what actually gets logged. MODEL_GENERAL/
#: MODEL_FAST are configured in production to Google's rolling aliases
#: ("gemini-pro-latest"/"gemini-flash-latest"), never the dated "gemini-2.5-*"
#: names, so every real Gemini call silently missed this table and fell through
#: to _DEFAULT_PRICE (Claude Sonnet 5's rate) -- confirmed live: 19
#: gemini-pro-latest + 4 gemini-flash-latest calls, zero of which had ever
#: matched a key here. deepseek-v4-pro was simply never added when CODE_REPAIR
#: was routed to it, so every real DeepSeek call was *also* silently priced at
#: Sonnet 5's rate -- directly undermining the reason DeepSeek was chosen.
#: deepseek-v4-pro pricing verified against deepseek.ai/pricing 2026-08-18;
#: its real cache-hit rate ($0.003625/M, ~0.83% of cache-miss) is far below
#: this file's blanket _CACHE_READ_MULTIPLIER (10%), calibrated for Anthropic --
#: a smaller, known residual overstatement on DeepSeek's cached input tokens
#: specifically, not fixed here.
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-pro-latest": (1.25, 10.00),
    "gemini-flash-latest": (0.30, 2.50),
    # Peak rates, deliberately, for both DeepSeek entries. The published prices are
    # time-of-day dependent (peak 01:00-04:00 and 06:00-10:00 UTC Mon-Fri, off-peak
    # otherwise) and this table has no time dimension. A cost *guard* must never
    # under-estimate -- an over-estimate stops work early and is visible, an
    # under-estimate silently lets a cap be exceeded -- so the higher rate wins.
    # Verified against api-docs.deepseek.com/quick_start/pricing 2026-09-13;
    # off-peak is flash (0.15, 0.60) and pro (0.66, 1.98).
    "deepseek-flash": (0.30, 1.20),
    "deepseek-v4-pro": (1.32, 3.96),
}

_DEFAULT_PRICE = (3.00, 15.00)
_CACHE_READ_MULTIPLIER = 0.1
_CACHE_WRITE_MULTIPLIER = 1.25


@dataclass(slots=True)
class CostRow:
    provider: str
    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    usd: float


def estimate_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """``input_tokens`` must already be *exclusive* of cached tokens — the same
    convention Anthropic's API uses natively. Gemini's raw ``prompt_token_count`` is
    *inclusive* of cached tokens instead, so ``GeminiProvider`` normalizes it at the
    usage boundary before it ever reaches this function. One formula cannot serve
    both conventions, so there is exactly one convention and exactly one place that
    enforces it."""
    if model not in PRICE_TABLE:
        # Loud, because silence here has now cost three times. A missing key does not
        # fail -- it quietly prices the call at Claude Sonnet's rate, which is a
        # 13x overstatement for deepseek-flash and inverts every decision made from
        # the number: a repair agent cut off by a cost cap it never actually reached,
        # a breaker reading spend that did not happen, and a POC whose entire
        # cost rationale is invisible in its own metrics.
        log.warning("costs.model_not_priced", model=model, fallback_usd_per_m=_DEFAULT_PRICE)
    price_in, price_out = PRICE_TABLE.get(model, _DEFAULT_PRICE)
    cost = (
        (input_tokens / 1_000_000) * price_in
        + (cache_read_tokens / 1_000_000) * price_in * _CACHE_READ_MULTIPLIER
        + (cache_write_tokens / 1_000_000) * price_in * _CACHE_WRITE_MULTIPLIER
        + (output_tokens / 1_000_000) * price_out
    )
    return round(cost, 6)


async def spend_by_model(session: AsyncSession, *, hours: int = 24) -> list[CostRow]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    stmt = (
        select(
            LLMCall.provider,
            LLMCall.model,
            func.count().label("calls"),
            func.coalesce(func.sum(LLMCall.input_tokens), 0),
            func.coalesce(func.sum(LLMCall.output_tokens), 0),
            func.coalesce(func.sum(LLMCall.cache_read_tokens), 0),
            func.coalesce(func.sum(LLMCall.cache_write_tokens), 0),
            func.coalesce(func.sum(LLMCall.cost_usd), 0.0),
        )
        .where(LLMCall.created_at >= since)
        .group_by(LLMCall.provider, LLMCall.model)
        .order_by(func.count().desc())
    )
    rows = (await session.execute(stmt)).all()
    return [
        CostRow(
            provider=provider,
            model=model,
            calls=int(calls),
            input_tokens=int(tokens_in),
            output_tokens=int(tokens_out),
            cache_read_tokens=int(cache_read),
            cache_write_tokens=int(cache_write),
            usd=round(float(usd), 6),
        )
        for provider, model, calls, tokens_in, tokens_out, cache_read, cache_write, usd in rows
    ]


async def total_usd(session: AsyncSession, *, hours: int = 24) -> float:
    return round(sum(row.usd for row in await spend_by_model(session, hours=hours)), 4)


#: Outcomes that mean the call ultimately produced a usable result --
#: "schema_violation_repaired" means the client's own repair pass recovered,
#: not that the provider failed. Everything else ("refusal", "rejected",
#: "schema_violation", "unavailable") is a real failure for this purpose.
_SUCCESS_OUTCOMES = ("ok", "schema_violation_repaired")


async def provider_outcome_rates(
    session: AsyncSession, *, minutes: int = 30
) -> dict[str, tuple[int, int]]:
    """``(errors, total)`` per provider over a trailing window -- there is no
    provider health-check/ping anywhere in this codebase, so "is Anthropic/
    Gemini currently up" has to be inferred from how recent real calls
    actually went, not measured directly."""
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    stmt = (
        select(
            LLMCall.provider,
            func.count().label("total"),
            func.coalesce(
                func.sum(case((LLMCall.outcome.notin_(_SUCCESS_OUTCOMES), 1), else_=0)), 0
            ),
        )
        .where(LLMCall.created_at >= since)
        .group_by(LLMCall.provider)
    )
    rows = (await session.execute(stmt)).all()
    return {str(provider): (int(errors), int(total)) for provider, total, errors in rows}
