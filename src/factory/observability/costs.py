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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from factory.state.orm import LLMCall

#: USD per million tokens, (input, output). Update alongside provider pricing.
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
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
