"""Task-type → provider routing.

A static dict, never a model decision. Getting this wrong would mean code silently
written by the wrong model, so the mapping is data that a test can assert over.
"""

from __future__ import annotations

from factory.llm.providers.base import LLMProvider, Provider
from factory.settings import Settings, TaskType, get_settings

#: Gemini handles general reasoning; Claude handles anything that produces or
#: judges source code via strict structured output; DeepSeek handles agentic
#: repair/healing (tool-calling only -- see TaskType.CODE_REPAIR's docstring in
#: settings.py for why it's split out from CODE_GENERATION rather than sharing it).
ROUTING: dict[TaskType, Provider] = {
    TaskType.ORCHESTRATION: Provider.GEMINI,
    TaskType.DISCOVERY: Provider.GEMINI,
    TaskType.ANALYSIS: Provider.GEMINI,
    TaskType.COMPLIANCE: Provider.GEMINI,
    TaskType.MARKETING_COPY: Provider.GEMINI,
    # Anthropic because its provider is the one that actually sends image
    # blocks. The others accept `images` and ignore them, which is the right
    # degradation for a text call and the wrong answer entirely for this one.
    TaskType.VISUAL_REVIEW: Provider.ANTHROPIC,
    TaskType.LANDING_PAGE: Provider.DEEPSEEK,
    TaskType.CODE_GENERATION: Provider.ANTHROPIC,
    TaskType.CODE_ITERATION: Provider.DEEPSEEK,
    TaskType.CODE_REVIEW: Provider.ANTHROPIC,
    TaskType.CODE_REPAIR: Provider.DEEPSEEK,
}

#: Code tasks must never be silently served by the other vendor: a cross-provider
#: failover here would change *who wrote the code that ships*, invisibly.
NO_FAILOVER: frozenset[TaskType] = frozenset(
    {
        TaskType.LANDING_PAGE,
        TaskType.CODE_GENERATION,
        TaskType.CODE_ITERATION,
        TaskType.CODE_REVIEW,
        TaskType.CODE_REPAIR,
        # Never fail a visual judgement over to a provider that cannot see. It would
        # not error -- it would answer, from the markup, sounding exactly like a real
        # answer. That is worse than the outage it avoids.
        TaskType.VISUAL_REVIEW,
    }
)

#: DeepSeek is a dedicated, single-purpose provider (CODE_REPAIR only, itself in
#: NO_FAILOVER) -- it must never become an accidental fallback for some *other*
#: task type's outage. `ProviderRegistry.alternatives()` has no concept of "is
#: this provider appropriate for this task," so without this filter a
#: Gemini-routed task (ANALYSIS, ORCHESTRATION, COMPLIANCE, ...) failing over
#: would treat every other registered provider as a candidate, including
#: DeepSeek -- sending it a model name it has never heard of (whatever
#: model_general/model_fast resolves to for the *primary* provider). Confirmed
#: live: one real ANALYSIS call reached DeepSeek this way and was correctly
#: rejected for an unrecognised model, but a more lenient endpoint could have
#: silently served a compliance or orchestration decision on the wrong model
#: entirely. Only the two general-reasoning providers may stand in for each
#: other; DeepSeek can only ever be reached as CODE_REPAIR's own primary.
GENERAL_FALLBACK_PROVIDERS: frozenset[Provider] = frozenset({Provider.GEMINI, Provider.ANTHROPIC})


#: Task types `POC_MODEL_OVERRIDE` does **not** capture -- they keep their normal
#: `ROUTING` entry even in POC mode.
#:
#: The three that write code which ships. Everything else -- orchestration,
#: analysis, compliance, discovery, marketing copy, review and agentic repair --
#: runs on the POC model, which is where a cheap model is most informative: the POC
#: exists to find *structural* gaps (thin briefs, dropped payloads, caps), and those
#: show up in plumbing rather than in raw generation ability.
#:
#: Each keeps its own routing for its own reason:
#: - CODE_GENERATION is one shot at an entire product from nothing, and is the place
#:   model quality shows most.
#: - CODE_ITERATION is editing a product that already exists, which is materially
#:   easier and does not warrant the same model.
#: - LANDING_PAGE is a page with a form, easier still, but it is real shipped code.
#:
#: CODE_REVIEW is deliberately NOT exempt. Jesse's reasoning, and it is right: if
#: the system works as it should, who reviews the code should not matter. Review is
#: a judgement over evidence already gathered, which is exactly what the POC is
#: meant to stress.
POC_EXEMPT: frozenset[TaskType] = frozenset(
    {
        TaskType.LANDING_PAGE,
        TaskType.CODE_GENERATION,
        TaskType.CODE_ITERATION,
        # The POC override points everything at deepseek-flash, which this provider
        # layer cannot send images to. Exempting it keeps a visual review on a model
        # with eyes even while the cheap-model mode is on.
        TaskType.VISUAL_REVIEW,
    }
)


def _poc_model(task_type: TaskType, settings: Settings) -> str | None:
    """The POC override in force for this task type, or None if it does not apply."""
    if not settings.poc_model_override or task_type in POC_EXEMPT:
        return None
    return settings.poc_model_override


def provider_for(task_type: TaskType, settings: Settings | None = None) -> Provider:
    """Which vendor serves this task type.

    `POC_MODEL_OVERRIDE` sends every task type except `POC_EXEMPT` to DeepSeek --
    see `model_for` for what that mode is and why it does not weaken `NO_FAILOVER`.
    """
    settings = settings or get_settings()
    if _poc_model(task_type, settings):
        return Provider.DEEPSEEK
    try:
        return ROUTING[task_type]
    except KeyError as exc:  # pragma: no cover - unreachable while the enum is exhaustive
        raise KeyError(f"no provider configured for task type {task_type!r}") from exc


def may_failover(task_type: TaskType) -> bool:
    return task_type not in NO_FAILOVER


def model_for(task_type: TaskType, settings: Settings | None = None) -> str:
    """Model id per task type.

    Orchestration runs on every tick and produces a tightly-constrained decision, so
    it uses the fast model; everything judgement-heavy uses the general model.

    `POC_MODEL_OVERRIDE` replaces all of that with one model for every task type
    except `POC_EXEMPT`. It exists because two days of live debugging produced
    twelve bugs and not one of them was a reasoning failure -- they were caps,
    orderings and dropped payloads. A cheap model exposes that class of problem
    *better* than a strong one, which papers over a thin brief by inferring what
    was meant.

    This deliberately overrides the ROUTING table above, including its
    Anthropic-only code paths. It does **not** weaken `NO_FAILOVER`: that rule
    exists so code is never *silently* served by a vendor nobody chose, and this is
    a single explicit vendor chosen by an operator setting one value. Nothing falls
    back, and clearing the setting restores the table exactly as written.
    """
    settings = settings or get_settings()
    poc = _poc_model(task_type, settings)
    if poc:
        return poc
    if task_type is TaskType.ORCHESTRATION:
        return settings.model_fast
    provider = provider_for(task_type, settings)
    if provider is Provider.ANTHROPIC:
        return settings.model_code
    if task_type in (TaskType.LANDING_PAGE, TaskType.CODE_ITERATION):
        return settings.model_deepseek_pro
    if provider is Provider.DEEPSEEK:
        return settings.model_deepseek
    return settings.model_general


def max_tokens_for(task_type: TaskType) -> int:
    if task_type in (TaskType.CODE_GENERATION, TaskType.CODE_ITERATION):
        # 32_000 was observed live to be too tight: Claude Sonnet 5's adaptive
        # thinking is not budget-capped (budget_tokens is rejected outright on this
        # model), and an open-ended "produce the complete MVP" prompt let thinking
        # alone consume the entire 32k, hitting stop_reason="max_tokens" with a
        # single "thinking" content block and zero answer text -- every build_mvp
        # attempt failed with SchemaViolation("claude returned no text content")
        # this way. 64_000 is Claude Sonnet 5's supported ceiling (confirmed against
        # the live API) and the only lever available to give thinking and the final
        # JSON answer enough combined room.
        return 64_000
    if task_type is TaskType.LANDING_PAGE:
        # Was 32_000, on the reasoning that a page is not an application. That was an
        # assumption and it measured wrong: a real run truncated mid-plan, emitting
        # the eighth file's path with no contents and no test_command at all. The
        # generator does not produce "a page" -- LANDING_PAGE_SYSTEM asks for a
        # backend, a test suite, a privacy page, a sitemap and robots.txt, and
        # index.html alone came to 22KB.
        #
        # Matched to CODE_GENERATION rather than guessed again. A landing page is
        # genuinely smaller than an MVP, so this has real headroom rather than being
        # a number tuned to the one case that failed.
        #
        # The deeper fix is upstream and is a prompt change, not a budget: a
        # validation page whose own requirements list "a working product" as out of
        # scope should not be generating a FastAPI backend at all.
        return 64_000
    if task_type is TaskType.CODE_REVIEW:
        return 16_000
    if task_type is TaskType.CODE_REPAIR:
        # Same ceiling as CODE_GENERATION -- WORKSPACE_SURGEON/SEO_OPTIMIZER/
        # REPAIR_ENGINEER got this budget when they were still routed under
        # CODE_GENERATION; splitting the task type out is a routing change,
        # not a reason to also shrink their existing headroom.
        return 64_000
    if task_type is TaskType.ORCHESTRATION:
        return 2_048
    return 8_192


class ProviderRegistry:
    """Holds one instance per provider and answers availability questions."""

    def __init__(self, providers: dict[Provider, LLMProvider]) -> None:
        self._providers = providers

    def get(self, provider: Provider) -> LLMProvider | None:
        instance = self._providers.get(provider)
        if instance is None or not instance.available:
            return None
        return instance

    def alternatives(self, exclude: Provider) -> list[LLMProvider]:
        return [p for name, p in self._providers.items() if name is not exclude and p.available]

    def available_providers(self) -> list[Provider]:
        return [name for name, p in self._providers.items() if p.available]

    def items(self) -> list[tuple[Provider, LLMProvider]]:
        """Every configured provider, available or not — used to wrap each one
        identically (e.g. for metering) without caring which are currently live."""
        return list(self._providers.items())
