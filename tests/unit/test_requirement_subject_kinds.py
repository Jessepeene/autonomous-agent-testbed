"""Every subject kind the code writes must be one the database will accept.

The guard for a bug that cost a finished piece of work three times over.

`record_design_guidelines` writes `subject_kind='design'`; the CHECK constraint
from migration 0034 allowed only ('product','task','issue','campaign'). Nothing
caught it, and nothing could have caught it by reading the code -- the mismatch is
between a Python string literal and a SQL constraint, and neither file mentions the
other.

It also could not fail until very late. The Design manager reported "nothing worth
doing" on every tick it ever ran, so the write was never reached. The first tick
that commissioned work hit the wall immediately: real guidelines were generated,
rejected by the database, and regenerated and rejected twice more before the task
escalated with the work discarded each time.

So the test is not "design is allowed". It is that the two lists agree, whichever
kind somebody adds next.
"""

from __future__ import annotations

import ast
import pathlib
import re

from factory.state.orm import Requirement

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "factory"


def _permitted_kinds() -> set[str]:
    """The kinds the database will accept, read off the constraint itself."""
    for constraint in Requirement.__table__.constraints:
        text = str(getattr(constraint, "sqltext", ""))
        if "subject_kind" in text:
            return set(re.findall(r"'([a-z_]+)'", text))
    raise AssertionError("no subject_kind check constraint found on requirements")


def _written_kinds() -> dict[str, str]:
    """Every literal `subject_kind=` the codebase actually writes, by file."""
    found: dict[str, str] = {}
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not parseable, not our concern
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.keyword) or node.arg != "subject_kind":
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                found[node.value.value] = path.name
    return found


def test_every_subject_kind_the_code_writes_is_one_the_database_accepts() -> None:
    permitted = _permitted_kinds()
    written = _written_kinds()

    assert written, "found no subject_kind writes at all -- the scan is broken, not the code"

    rejected = {kind: where for kind, where in written.items() if kind not in permitted}
    assert not rejected, (
        f"these are written by the code and rejected by the database: {rejected}. "
        f"The constraint permits {sorted(permitted)}. Widen it in a migration -- a row "
        "that cannot be stored destroys the work that produced it, after paying for it."
    )


def test_the_scan_actually_sees_the_write_that_caused_this() -> None:
    """Pins the guard itself.

    A scan that silently stopped finding anything would pass forever. This is the
    specific write that was rejected in production, so if it is no longer visible
    the detector has broken rather than the problem having gone away.
    """
    assert _written_kinds().get("design") == "repository.py"
