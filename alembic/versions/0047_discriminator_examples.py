"""Append a generalising clause to the discriminator's adoption-ease examples, so
an already-bootstrapped database keeps serving it -- not just a freshly bootstrapped
one.

Revision ID: 0047_discriminator_examples
Revises: 0046_objective
Create Date: 2026-09-27

Same reasoning as `0023_idea_defensibility.py` and `0028_tier0_prompt_appendices.py`
(see their own module docstrings): `learning/prompts.py::get_prompt` resyncs a
*bootstrap* copy of a tunable prompt to the current code constant automatically on
every read, but a *learned* row (one a trial actually promoted) wins over the code
and escalates instead -- so a database that already has a real, previously-promoted
`active` row for this key needs the addition recorded here explicitly, the same way
0028 recorded its own additions, rather than assuming the generic resync alone
covers every future edit.

Found auditing every prompt in the codebase for accidentally narrow examples
(`ARCHITECTURE_ROADMAP.md`, "Neutral prompting", 2026-09-26/27): `adoption_ease`
named "construction, healthcare, government, and heavy industry" as low-adoption-ease
examples without a generalising clause, so a model reading it could read the named
four as the definition rather than as illustrations of the stated traits (long
procurement cycles, sign-off requirements, low tech adoption, a paper-and-phone
culture). Same mechanism as the Tailwind palette ban's original defect, applied to a
scoring dimension instead of a style guide.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0047_discriminator_examples"
down_revision: str | None = "0046_objective"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DISCRIMINATOR_KEY = "factory.engines.discovery.score:DISCRIMINATOR_SYSTEM"

# Byte-identical to the new paragraph appended to the live
# `discovery/score.py::DISCRIMINATOR_SYSTEM` constant.
_GENERALISATION_APPENDIX = """

One note on the named industries above: construction, healthcare, government, and \
heavy industry are examples of the traits described, not the definition of \
low-adoption-ease. Judge the industry actually in front of you against those traits \
-- long procurement, sign-off requirements, low tech adoption, a paper-and-phone \
culture -- rather than against that list; plenty of unnamed industries share them \
and deserve the same low score."""


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
    _append_to_active_row(conn, tunable, _DISCRIMINATOR_KEY, _GENERALISATION_APPENDIX)


def downgrade() -> None:
    # Deliberately a no-op, matching 0028: stripping a suffix back off a row that may
    # since have accumulated further learned changes on top of it is not a safe
    # inverse, and nothing downstream depends on the addition being removable.
    pass
