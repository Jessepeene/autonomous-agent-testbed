"""The chokepoint: which prompts and scoring weights the factory may ever mutate.

Every key here is commercially-lossy, never safety-critical — a bad discovery prompt
means worse ideas queued (caught by the compliance/dev gates downstream anyway), a bad
outreach prompt means fewer replies, a bad landing-page prompt means a weaker pitch.
None of these seven keys can produce a compliance violation, a security hole, or
unlawful data processing on their own. The LIA prompts, Bug Council reviewer prompts,
the orchestrator's system prompt, and the real-MVP codegen prompt
(``factory.engines.development.scaffold.generator:SYSTEM_PROMPT`` specifically — not
every constant in that module, see below) are permanently excluded, by omission — they
are simply never on this list, and nothing in ``factory.engines.compliance``,
``factory.engines.development.bug_council``, or ``factory.supervisor.orchestrator``
ever imports this package.

``key`` is always ``module.path:ATTRIBUTE_NAME``, never a bare attribute name: three
unrelated constants in this codebase are named ``SYSTEM_PROMPT`` or similar
(``factory.engines.marketing.outreach:SYSTEM_PROMPT`` — mutable — and
``factory.engines.development.scaffold.generator:SYSTEM_PROMPT`` — the real-MVP
prompt, permanently excluded, not coincidentally absent from this set — versus that
same module's ``LANDING_PAGE_SYSTEM``, a different constant with a different risk
profile: it never writes auth, billing, or business logic, only a pitch page, so it is
deliberately mutable here).
"""

from __future__ import annotations

MUTABLE_KEYS: frozenset[str] = frozenset(
    {
        "factory.engines.discovery.score:GENERATOR_SYSTEM",
        "factory.engines.discovery.score:DISCRIMINATOR_SYSTEM",
        "factory.engines.discovery.score:SCORING_WEIGHTS",
        "factory.engines.marketing.leads:PLANNER_SYSTEM",
        "factory.engines.marketing.outreach:SYSTEM_PROMPT",
        "factory.engines.development.scaffold.generator:LANDING_PAGE_SYSTEM",
        "factory.engines.marketing.outreach:SENDER_IDENTITY",
    }
)

#: One outcome metric a trial evaluator can gate on per key. `outreach_content_pass_rate`
#: is the *primary* gate for the outreach prompt, not `outreach_conversion_rate` — see
#: `learning/trials.py` for the statistical-power reasoning: a realistic conversion
#: baseline is too low-volume to reliably catch a real regression within one trial.
#: `landing_page_interest_rate` is its own metric, deliberately never mixed into
#: `outreach_content_pass_rate`/`planner_effective_rate`'s episode pools — see
#: `EVALUATE_VALIDATION` in `engines/development/graph.py` for why: episodes carry no
#: metric/kind tag, so pooling a different Bernoulli population into an existing key's
#: statistics would silently corrupt that key's own trial judgments.
#:
#: `SENDER_IDENTITY` is the first key actually gated on `outreach_conversion_rate` —
#: there is no faster proxy for "did this name work" the way `outreach_content_pass_rate`
#: proxies for prompt quality, since the name itself has no synchronous pass/fail. Its
#: outcome is recorded later, on a delay, by `marketing/attribution.py::reconcile` —
#: see that module and `state/repository.py::outreach_needing_identity_timeout`.
DEFAULT_METRIC: dict[str, str] = {
    "factory.engines.discovery.score:GENERATOR_SYSTEM": "idea_survival_rate",
    "factory.engines.discovery.score:DISCRIMINATOR_SYSTEM": "idea_survival_rate",
    "factory.engines.discovery.score:SCORING_WEIGHTS": "idea_survival_rate",
    "factory.engines.marketing.leads:PLANNER_SYSTEM": "planner_effective_rate",
    "factory.engines.marketing.outreach:SYSTEM_PROMPT": "outreach_content_pass_rate",
    "factory.engines.development.scaffold.generator:LANDING_PAGE_SYSTEM": (
        "landing_page_interest_rate"
    ),
    "factory.engines.marketing.outreach:SENDER_IDENTITY": "outreach_conversion_rate",
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
