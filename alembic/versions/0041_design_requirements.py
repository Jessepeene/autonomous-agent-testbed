"""Let a design guideline be stored at all.

`record_design_guidelines` writes into `requirements` with
``subject_kind='design'`` -- deliberately, because guidelines are the same kind of
thing as acceptance criteria: a standard written before the work, that the builder
works to and a reviewer judges against, superseded rather than rewritten.

But `0034_requirements` constrained `subject_kind` to
``('product','task','issue','campaign')``, so every such write was rejected by the
database.

**It stayed invisible because the department never got far enough to fail.** The
Design manager shipped alongside this and reported "nothing worth doing" on every
tick -- first because it could not ask the question it needed answered, then
because its domain query only looked at products that had already shipped. The
first tick that actually commissioned work found the wall immediately: the agent
wrote real guidelines (a palette, a typographic scale, a stated intent), the insert
raised `CheckViolationError`, and the task burned all three attempts and escalated
with the finished work discarded each time.

That is the expensive shape of this bug. Nothing was "broken" in a way any test
could see, because the only path that reaches the write is one no test and no live
run had ever taken.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0041_design_requirements"
down_revision: str | None = "0040_questions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The bare name, not the one that appears in the database. Alembic applies this
#: project's `ck_%(table_name)s_%(constraint_name)s` convention itself, so passing
#: the expanded `ck_requirements_requirement_subject_kind_valid` gets it expanded a
#: second time and the DROP looks for a constraint that never existed.
_NAME = "requirement_subject_kind_valid"
_OLD = "subject_kind IN ('product','task','issue','campaign')"
_NEW = "subject_kind IN ('product','task','issue','campaign','design')"


def upgrade() -> None:
    op.drop_constraint(_NAME, "requirements", type_="check")
    op.create_check_constraint(_NAME, "requirements", _NEW)


def downgrade() -> None:
    # Rows written under the widened constraint would fail the narrow one, so they
    # are removed first. Safe in the only direction that matters: a design guideline
    # is regenerated from the product it describes, never hand-authored, and keeping
    # a row the schema forbids would make the downgrade itself fail.
    op.execute("DELETE FROM requirements WHERE subject_kind = 'design'")
    op.drop_constraint(_NAME, "requirements", type_="check")
    op.create_check_constraint(_NAME, "requirements", _OLD)
