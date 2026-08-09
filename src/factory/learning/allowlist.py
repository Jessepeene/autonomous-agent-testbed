"""The chokepoint: which prompts and scoring weights the factory may ever mutate.

Every key here is commercially-lossy, never safety-critical — a bad discovery prompt
means worse ideas queued (caught by the compliance/dev gates downstream anyway), a bad
outreach prompt means fewer replies. None of these four keys can produce a compliance
violation, a security hole, or unlawful data processing on their own. The LIA prompts,
Bug Council reviewer prompts, the orchestrator's system prompt, and the codegen scaffold
prompt are permanently excluded, by omission — they are simply never on this list, and
nothing in ``factory.engines.compliance``, ``factory.engines.development.bug_council``,
``factory.supervisor.orchestrator``, or ``factory.engines.development.scaffold`` ever
imports this package.

``key`` is always ``module.path:ATTRIBUTE_NAME``, never a bare attribute name: two
unrelated constants in this codebase are both literally named ``SYSTEM_PROMPT``
(``factory.engines.marketing.outreach:SYSTEM_PROMPT`` — mutable — and
``factory.engines.development.scaffold.generator:SYSTEM_PROMPT`` — permanently
excluded, and not coincidentally absent from this set).
"""

from __future__ import annotations

MUTABLE_KEYS: frozenset[str] = frozenset(
    {
        "factory.engines.discovery.score:GENERATOR_SYSTEM",
        "factory.engines.discovery.score:DISCRIMINATOR_SYSTEM",
        "factory.engines.discovery.score:SCORING_WEIGHTS",
        "factory.engines.marketing.leads:PLANNER_SYSTEM",
        "factory.engines.marketing.outreach:SYSTEM_PROMPT",
    }
)

#: One outcome metric a trial evaluator can gate on per key. `outreach_content_pass_rate`
#: is the *primary* gate for the outreach prompt, not `outreach_conversion_rate` — see
#: `learning/trials.py` for the statistical-power reasoning: a realistic conversion
#: baseline is too low-volume to reliably catch a real regression within one trial.
DEFAULT_METRIC: dict[str, str] = {
    "factory.engines.discovery.score:GENERATOR_SYSTEM": "idea_survival_rate",
    "factory.engines.discovery.score:DISCRIMINATOR_SYSTEM": "idea_survival_rate",
    "factory.engines.discovery.score:SCORING_WEIGHTS": "idea_survival_rate",
    "factory.engines.marketing.leads:PLANNER_SYSTEM": "planner_effective_rate",
    "factory.engines.marketing.outreach:SYSTEM_PROMPT": "outreach_content_pass_rate",
}


class ProtectedKey(ValueError):
    """A caller tried to mutate a key that is not on the allowlist."""


def assert_mutable(key: str) -> None:
    if key not in MUTABLE_KEYS:
        raise ProtectedKey(f"{key!r} is not on the mutation allowlist")


def default_metric_for(key: str) -> str:
    """Every mutable key has exactly one default target metric — this is data, not a
    judgement call an agent or an operator makes per mutation."""
    assert_mutable(key)
    return DEFAULT_METRIC[key]
