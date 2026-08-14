"""Add the defensibility scoring dimension, and rewrite live SCORING_WEIGHTS /
DISCRIMINATOR_SYSTEM rows to match.

Revision ID: 0023_idea_defensibility
Revises: 0022_pypi_package_checks
Create Date: 2026-08-14

``ScoredIdea`` gains a fifth weighted dimension (``defensibility``) plus two
informational, ungated enums (``platform_dependency``, ``replacement_mode``) --
see ``discovery/score.py``'s rewritten ``DISCRIMINATOR_SYSTEM`` and
``schemas.py``'s ``ScoredIdea.SCORE_DIMENSIONS``.

This must be the same migration as that schema change, not a follow-up: once
the code ships, every *newly bootstrapped* database gets the new 5-key default
weights and the new prompt automatically -- but ``learning/prompts.py::get_prompt``
/``get_scoring_weights`` only ever bootstrap once and then serve whatever's
already in ``tunable_parameters`` forever, with no re-sync path. A database that
already has an ``active`` row for either key (this one, and every database that
has run a single Discovery cycle) would otherwise keep serving a 4-dimension
weights dict and the old prompt text forever, silently: ``weighted_score``'s
``weights.get(dim, 0.0)`` would just zero out ``defensibility`` for every idea,
changing every score without anyone deciding that.

Two data rewrites, each branching on whether the live row was ever touched by
the learning loop:

* ``SCORING_WEIGHTS`` -- still the untouched code default -> replaced outright
  with the new 5-key default. A *learned* value (already promoted through a
  won trial) -> every existing weight scaled by 0.82 and ``defensibility``
  inserted at exactly 0.18, so the scaled vector sums back to 1.0 (calibrated
  against the fixed ``SCORE_THRESHOLD = 0.55``) while preserving the *relative*
  ordering the learning loop already discovered between the four original
  dimensions -- a uniform scalar multiplier changes every weight's absolute
  value but none of their ratios.
* ``DISCRIMINATOR_SYSTEM`` -- still the untouched code constant -> replaced
  outright with the new constant. Custom/learned prose -> the new instructions
  are *appended*, never used to overwrite tuned prose that might no longer
  match the code constant word-for-word.

Any ``candidate``/``trial`` row for either key is retired (never deleted): a
trial that started collecting episodes under the 4-dimension contract is
statistically uninterpretable once the contract underneath it has changed
mid-flight, and `promote_and_evaluate` must be free to start a fresh trial on
the new 5-dimension plane rather than resolve a contaminated one. The
``active`` row itself is never retired, even when rewritten in place --
``bootstrap_active`` hardcodes ``version=1`` and every reader asserts an
``active`` row exists; a gap would crash the next Discovery cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0023_idea_defensibility"
down_revision: str | None = "0022_pypi_package_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WEIGHTS_KEY = "factory.engines.discovery.score:SCORING_WEIGHTS"
_DISCRIMINATOR_KEY = "factory.engines.discovery.score:DISCRIMINATOR_SYSTEM"

_OLD_DEFAULT_WEIGHTS: dict[str, float] = {
    "pain_severity": 0.3,
    "willingness_to_pay": 0.3,
    "adoption_ease": 0.25,
    "build_feasibility": 0.15,
}
_NEW_DEFAULT_WEIGHTS: dict[str, float] = {
    "pain_severity": 0.25,
    "willingness_to_pay": 0.25,
    "adoption_ease": 0.20,
    "build_feasibility": 0.12,
    "defensibility": 0.18,
}
_LEARNED_WEIGHT_SCALE = 0.82
_DEFENSIBILITY_WEIGHT = 0.18

# Byte-identical to the pre-Phase-4 `discovery/score.py::DISCRIMINATOR_SYSTEM`
# constant -- used only to detect an untouched row; reproduced here verbatim
# (same source-level backslash line-continuations) rather than hand-composed,
# so it evaluates to the exact same runtime string the original constant did.
_OLD_DISCRIMINATOR_SYSTEM = """\
You are the discriminator. Your job is to kill weak ideas before anyone builds them, \
and to attach a concrete monetization hypothesis to the survivors.

For each idea, score four independent dimensions from 0.0 to 1.0:
- pain_severity: how badly this hurts today. Workarounds that cost hours weekly score \
high; mild annoyances score low.
- willingness_to_pay: whether this buyer has budget authority and an existing habit of \
paying for tools. Score low when the sufferer cannot authorise spend.
- build_feasibility: whether a small team could ship a useful v1 in days, using public \
APIs, with no proprietary data and no regulated activity.
- adoption_ease: whether this buyer can actually start using and paying for new \
software alone, independent of pain or price. Score high when the buyer already \
discovers and buys similar self-serve tools online without anyone's approval. Score \
low for industries with long procurement cycles, safety or compliance sign-off \
requirements, low tech adoption, or a cultural preference for phone calls and paper \
over self-serve checkout -- construction, healthcare, government, and heavy industry \
are common examples. A high-pain, high-budget idea in a low-adoption-ease industry is \
still a bad bet: the sale never closes regardless of how real the pain is.

monetization_hypothesis must name who pays, roughly how much per month, and what \
triggers the purchase. "Subscription" is not a hypothesis.

This factory has no sales team and no onboarding staff: every product must sell and \
onboard itself. For every idea also set:
- requires_human_sales: true if closing a paying customer plausibly needs a sales \
call, a demo, or price negotiation rather than a self-serve checkout.
- requires_human_onboarding: true if a customer cannot start getting value alone from \
signup, without a human walking them through setup, migrating their data, or \
configuring something on their behalf.
- self_serve_price_usd: the monthly price a self-serve buyer would pay without talking \
to anyone, in whole US dollars.

Score honestly and low. Optimistic scoring here wastes real engineering budget \
downstream. If you would not personally build it, say so in rejection_reason and score \
it accordingly.\
"""

# Byte-identical to the post-Phase-4 constant in `discovery/score.py` -- used only
# in the "row was untouched" replace branch.
_NEW_DISCRIMINATOR_SYSTEM = """\
You are the discriminator. Your job is to kill weak ideas before anyone builds them, \
and to attach a concrete monetization hypothesis to the survivors.

For each idea, score five independent dimensions from 0.0 to 1.0:
- pain_severity: how badly this hurts today. Workarounds that cost hours weekly score \
high; mild annoyances score low.
- willingness_to_pay: whether this buyer has budget authority and an existing habit of \
paying for tools. Score low when the sufferer cannot authorise spend.
- build_feasibility: whether a small team could ship a useful v1 in days, using public \
APIs, with no regulated activity.
- adoption_ease: whether this buyer can actually start using and paying for new \
software alone, independent of pain or price. Score high when the buyer already \
discovers and buys similar self-serve tools online without anyone's approval. Score \
low for industries with long procurement cycles, safety or compliance sign-off \
requirements, low tech adoption, or a cultural preference for phone calls and paper \
over self-serve checkout -- construction, healthcare, government, and heavy industry \
are common examples. A high-pain, high-budget idea in a low-adoption-ease industry is \
still a bad bet: the sale never closes regardless of how real the pain is.
- defensibility: what stops a competitor from cloning this in a weekend. Score high \
for ideas that accumulate proprietary data over time, create real workflow lock-in \
(the customer's process now depends on this specific tool), or impose a real \
switching cost once adopted. Score low for a thin wrapper around one obvious API call \
that any competent developer could rebuild from the same public documentation. \
Depending on one third-party platform for the product's core function pushes this \
score down, even if the idea is otherwise strong. Write defensibility_reasoning -- \
what specifically would stop a clone, and how platform-dependent the idea is -- \
before you commit to the number.

Also classify, for every idea:
- platform_dependency: "required" if the product cannot function without a specific \
third-party platform (an app-store extension, a marketplace integration); "optional" \
if it integrates with one but could stand alone; "none" if it depends on no single \
platform; "unknown" only if the evidence genuinely does not say.
- replacement_mode: "replaces" if it displaces an existing tool or manual process the \
buyer currently uses; "augments" if it sits alongside and enhances an existing tool or \
platform; "observes" if it only reports on or monitors something without changing how \
the buyer works; "unknown" only if the evidence genuinely does not say.

monetization_hypothesis must name who pays, roughly how much per month, and what \
triggers the purchase. "Subscription" is not a hypothesis.

This factory has no sales team and no onboarding staff: every product must sell and \
onboard itself. For every idea also set:
- requires_human_sales: true if closing a paying customer plausibly needs a sales \
call, a demo, or price negotiation rather than a self-serve checkout.
- requires_human_onboarding: true if a customer cannot start getting value alone from \
signup, without a human walking them through setup, migrating their data, or \
configuring something on their behalf.
- self_serve_price_usd: the monthly price a self-serve buyer would pay without talking \
to anyone, in whole US dollars.

Score honestly and low. Optimistic scoring here wastes real engineering budget \
downstream. If you would not personally build it, say so in rejection_reason and score \
it accordingly.\
"""

# Appended (never used to overwrite) when the live row is not byte-identical to
# _OLD_DISCRIMINATOR_SYSTEM -- i.e. a human or the learning loop already tuned it.
_DISCRIMINATOR_APPENDIX = """\

[Added by 0023_idea_defensibility] Score a fifth independent dimension, also 0.0 \
to 1.0:
- defensibility: what stops a competitor from cloning this in a weekend. Score high \
for ideas that accumulate proprietary data over time, create real workflow lock-in \
(the customer's process now depends on this specific tool), or impose a real \
switching cost once adopted. Score low for a thin wrapper around one obvious API \
call that any competent developer could rebuild from the same public documentation. \
Depending on one third-party platform for the product's core function pushes this \
score down, even if the idea is otherwise strong. Write defensibility_reasoning -- \
what specifically would stop a clone, and how platform-dependent the idea is -- \
before you commit to the number.

Also classify, for every idea:
- platform_dependency: "required" if the product cannot function without a specific \
third-party platform (an app-store extension, a marketplace integration); "optional" \
if it integrates with one but could stand alone; "none" if it depends on no single \
platform; "unknown" only if the evidence genuinely does not say.
- replacement_mode: "replaces" if it displaces an existing tool or manual process the \
buyer currently uses; "augments" if it sits alongside and enhances an existing tool \
or platform; "observes" if it only reports on or monitors something without changing \
how the buyer works; "unknown" only if the evidence genuinely does not say.\
"""


def _matches(value: dict[str, float], reference: dict[str, float]) -> bool:
    if set(value) != set(reference):
        return False
    return all(abs(float(value[k]) - reference[k]) < 1e-9 for k in reference)


def _rescale_learned_weights(current: dict[str, float]) -> dict[str, float]:
    """A uniform scalar multiplier changes every weight's absolute value but none
    of their *ratios* -- whatever the learning loop discovered about pain vs. WTP
    vs. feasibility vs. adoption survives exactly, while ``defensibility`` claims
    the remaining share so the vector sums back to 1.0."""
    rescaled = {k: round(v * _LEARNED_WEIGHT_SCALE, 6) for k, v in current.items()}
    rescaled["defensibility"] = _DEFENSIBILITY_WEIGHT
    return rescaled


def upgrade() -> None:
    op.add_column("ideas", sa.Column("pain_severity", sa.Float(), nullable=True))
    op.add_column("ideas", sa.Column("willingness_to_pay", sa.Float(), nullable=True))
    op.add_column("ideas", sa.Column("build_feasibility", sa.Float(), nullable=True))
    op.add_column("ideas", sa.Column("adoption_ease", sa.Float(), nullable=True))
    op.add_column("ideas", sa.Column("defensibility", sa.Float(), nullable=True))
    op.add_column("ideas", sa.Column("platform_dependency", sa.String(length=16), nullable=True))
    op.add_column("ideas", sa.Column("replacement_mode", sa.String(length=16), nullable=True))
    op.create_check_constraint(
        "ideas_platform_dependency_valid",
        "ideas",
        "platform_dependency IN ('unknown','none','optional','required') "
        "OR platform_dependency IS NULL",
    )
    op.create_check_constraint(
        "ideas_replacement_mode_valid",
        "ideas",
        "replacement_mode IN ('unknown','replaces','augments','observes') "
        "OR replacement_mode IS NULL",
    )

    conn = op.get_bind()
    tunable = sa.table(
        "tunable_parameters",
        sa.column("id", sa.String),
        sa.column("key", sa.String),
        sa.column("value", JSONB),
        sa.column("status", sa.String),
        sa.column("retired_at", sa.DateTime(timezone=True)),
        sa.column("retired_reason", sa.String),
    )

    active_weights = conn.execute(
        sa.select(tunable.c.id, tunable.c.value).where(
            tunable.c.key == _WEIGHTS_KEY, tunable.c.status == "active"
        )
    ).first()
    if active_weights is not None:
        current = {str(k): float(v) for k, v in dict(active_weights.value).items()}
        new_value = (
            dict(_NEW_DEFAULT_WEIGHTS)
            if _matches(current, _OLD_DEFAULT_WEIGHTS)
            else _rescale_learned_weights(current)
        )
        conn.execute(
            sa.update(tunable).where(tunable.c.id == active_weights.id).values(value=new_value)
        )

    active_disc = conn.execute(
        sa.select(tunable.c.id, tunable.c.value).where(
            tunable.c.key == _DISCRIMINATOR_KEY, tunable.c.status == "active"
        )
    ).first()
    if active_disc is not None:
        current_text = str(dict(active_disc.value).get("text", ""))
        new_text = (
            _NEW_DISCRIMINATOR_SYSTEM
            if current_text == _OLD_DISCRIMINATOR_SYSTEM
            else current_text + _DISCRIMINATOR_APPENDIX
        )
        conn.execute(
            sa.update(tunable)
            .where(tunable.c.id == active_disc.id)
            .values(value={"text": new_text})
        )

    conn.execute(
        sa.update(tunable)
        .where(
            tunable.c.key.in_([_WEIGHTS_KEY, _DISCRIMINATOR_KEY]),
            tunable.c.status.in_(["trial", "candidate"]),
        )
        .values(
            status="retired",
            retired_at=datetime.now(UTC),
            retired_reason=(
                "superseded by the Phase 4 5-dimension scoring contract "
                "(0023_idea_defensibility) -- episodes collected under the old "
                "contract are not interpretable against the new one"
            ),
        )
    )


def downgrade() -> None:
    # Schema-only downgrade, matching every prior migration in this project: the
    # tunable_parameters data rewrite above is not reversed here. There is no
    # reliable inverse for either branch -- unscaling a weight vector back to its
    # pre-0.82 values, or stripping an appended instruction block back out of
    # whatever a human or the learning loop has since further edited it into --
    # so the safe, honest choice is to leave the rewritten rows as they are (still
    # a valid 5-dimension contract) rather than fabricate a restoration that could
    # silently corrupt live-tuned prompt/weight state.
    op.drop_constraint("ideas_replacement_mode_valid", "ideas", type_="check")
    op.drop_constraint("ideas_platform_dependency_valid", "ideas", type_="check")
    op.drop_column("ideas", "replacement_mode")
    op.drop_column("ideas", "platform_dependency")
    op.drop_column("ideas", "defensibility")
    op.drop_column("ideas", "adoption_ease")
    op.drop_column("ideas", "build_feasibility")
    op.drop_column("ideas", "willingness_to_pay")
    op.drop_column("ideas", "pain_severity")
