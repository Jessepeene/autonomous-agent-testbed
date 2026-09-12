"""`hitl/interrupts.py::plain_language_error` -- rewrites raw/freeform technical
error text (test failures, stack traces) into a plain-English explanation for an
operator with no code access. Used by the 3 escalation sites that dump
genuinely unpredictable raw text: heal-cycle failures (`development/graph.py`),
infra failures, and generic task exhaustion (both `supervisor/graph.py`).
"""

from __future__ import annotations

from factory.hitl.interrupts import _PlainLanguageExplanation, plain_language_error
from factory.llm.providers.base import LLMError, Provider
from factory.llm.providers.fake import FakeProvider
from factory.supervisor.context import FactoryContext


async def test_returns_the_llms_explanation_on_success(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    gemini_fake.register(
        _PlainLanguageExplanation,
        _PlainLanguageExplanation(
            explanation=(
                "The checkout flow's Stripe signature check keeps failing, which "
                "usually means the webhook secret is misconfigured."
            )
        ),
    )

    raw_text = "AssertionError: assert 400 == 200\nSignature verification failed"
    result = await plain_language_error(
        ctx,
        raw_text,
        context="Automated repair on 'Checkout Tool' did not converge.",
    )

    assert result.startswith(
        "The checkout flow's Stripe signature check keeps failing, which usually "
        "means the webhook secret is misconfigured."
    )
    # Tier 3 finding #12: the raw text is always still there too, so a human
    # who suspects the paraphrase mischaracterized the real error has
    # something to check it against.
    assert "(Raw detail, for reference)" in result
    assert raw_text in result
    # The raw text and the context both reached the model.
    call = gemini_fake.calls[-1]
    assert "AssertionError" in call["prompt"]
    assert "Automated repair on 'Checkout Tool' did not converge." in call["prompt"]


async def test_falls_back_to_raw_text_on_llm_error(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    """An escalation must never be blocked by this call failing. A bare
    `LLMError` (unlike `ProviderUnavailable`) is not retried or failed over by
    `LLMClient.parse` -- it propagates on the first attempt, so one queued
    error is enough here."""
    gemini_fake.register(_PlainLanguageExplanation, LLMError(Provider.GEMINI, "schema violation"))

    result = await plain_language_error(
        ctx, "raw traceback text here", context="A task failed repeatedly."
    )

    assert "raw traceback text here" in result
    assert result.startswith("(Technical detail, unedited)")


async def test_raw_text_is_truncated_before_being_sent(
    ctx: FactoryContext, gemini_fake: FakeProvider
) -> None:
    gemini_fake.register(_PlainLanguageExplanation, _PlainLanguageExplanation(explanation="ok"))
    huge = "x" * 10_000

    await plain_language_error(ctx, huge, context="test")

    call = gemini_fake.calls[-1]
    assert len(call["prompt"]) < 5000
