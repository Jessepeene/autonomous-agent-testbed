"""Append Tier 0 holistic-check instructions to the two live, already-tunable
prompts they were added to, so an already-bootstrapped database keeps serving
them -- not just a freshly bootstrapped one.

Revision ID: 0028_tier0_prompt_appendices
Revises: 0027_integration_alerts
Create Date: 2026-08-23

Same reasoning as `0023_idea_defensibility.py` (see its own module docstring):
`learning/prompts.py::get_prompt` only ever bootstraps a tunable_parameters row
once and then serves whatever's already `active` forever, with no re-sync path.
A database that has already run a single Discovery cycle or sent a single
outreach email has a real `active` row for both keys below -- editing the code
constant alone would leave that row silently stuck on the old text forever,
and the two new fields it doesn't ask for (`gut_check_passed`,
`reads_like_a_real_person_wrote_it`) would simply never be exercised on this
database, even though both default safely to "no veto" so nothing crashes.

Simpler than 0023's rewrite, on purpose: both additions here are purely
additive holistic checks, not a dimension-count change coupled to a second
tunable row (like `defensibility` was coupled to `SCORING_WEIGHTS`) -- so
there is no "was this row still the untouched default" branch to take. It is
always safe to append, whether the live row is pristine or already tuned/
previously appended-to by an earlier migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0028_tier0_prompt_appendices"
down_revision: str | None = "0027_integration_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISCRIMINATOR_KEY = "factory.engines.discovery.score:DISCRIMINATOR_SYSTEM"
_OUTREACH_KEY = "factory.engines.marketing.outreach:SYSTEM_PROMPT"

# Byte-identical to the new paragraph appended to the live
# `discovery/score.py::DISCRIMINATOR_SYSTEM` constant (Tier 0 finding #0c,
# system-wide verification plan).
_DISCRIMINATOR_APPENDIX = """

Finally, step back from the five dimensions above and apply one holistic check: \
gut_check_passed should be false whenever an experienced operator would object to \
this as a real business on sight, even if every dimension above happened to score \
well -- the niche is not actually distinct from products already well known to \
exist, the monetization_hypothesis contradicts the target_niche or problem_statement \
stated earlier, or the idea is simply not a coherent business when considered as a \
whole rather than five separate numbers. Set gut_check_passed=true by default; only \
set it false with a concrete, specific objection in gut_check_reasoning -- a generic \
hedge ("this is risky") does not count."""

# Byte-identical to the new paragraph appended to the live
# `marketing/outreach.py::SYSTEM_PROMPT` constant (Tier 0 finding #0d,
# system-wide verification plan).
_OUTREACH_APPENDIX = """

Finally, step back from the copy you just wrote and apply one holistic check: would \
a real salesperson at this company be comfortable sending this exact email, to this \
exact recipient, under their own name? Set reads_like_a_real_person_wrote_it=false if \
it reads generic, robotic, over-eager, or like a template that was never actually \
customized to this recipient's role or company -- and say specifically what in \
tone_concern."""


def _append_to_active_row(conn: sa.Connection, tunable: sa.Table, key: str, appendix: str) -> None:
    active = conn.execute(
        sa.select(tunable.c.id, tunable.c.value).where(
            tunable.c.key == key, tunable.c.status == "active"
        )
    ).first()
    if active is None:
        return  # never bootstrapped on this database -- the code default already covers it
    current_text = str(dict(active.value).get("text", ""))
    if current_text.endswith(appendix):
        return  # idempotent: re-running this migration must not double-append
    conn.execute(
        sa.update(tunable)
        .where(tunable.c.id == active.id)
        .values(value={"text": current_text + appendix})
    )


def upgrade() -> None:
    conn = op.get_bind()
    tunable = sa.table(
        "tunable_parameters",
        sa.column("id", sa.String),
        sa.column("key", sa.String),
        sa.column("value", JSONB),
        sa.column("status", sa.String),
    )
    _append_to_active_row(conn, tunable, _DISCRIMINATOR_KEY, _DISCRIMINATOR_APPENDIX)
    _append_to_active_row(conn, tunable, _OUTREACH_KEY, _OUTREACH_APPENDIX)


def downgrade() -> None:
    # Data-only migration, no schema change -- and no reliable inverse: an
    # active row may have been further tuned since this ran, so blindly
    # stripping a fixed suffix could corrupt whatever now follows it. Same
    # honest choice as 0023's downgrade: leave the rewritten rows as they are.
    pass
