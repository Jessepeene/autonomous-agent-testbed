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


#: Which table each `subject_kind=` writer actually targets. `requirements` is not the
#: only table with that column any more -- `outcome_evidence` (migration 0044) uses the
#: same `(subject_kind, subject_id)` pattern, deliberately and with no CHECK constraint,
#: so that adding `code` or `workflow` as something this company may change about itself
#: costs an insert rather than a migration.
#:
#: An unrecognised callee is a **failure**, not an omission. Defaulting unknown writers
#: to "requirements" would resurrect the false positive this map removes; defaulting
#: them to "ignore" would silently reopen the hole the whole test exists to close. So a
#: new writer has to be classified here by whoever adds it.
_TARGETS: dict[str, str] = {
    "record_requirements": "requirements",
    "live_requirements": "requirements",
    "Requirement": "requirements",
    "record_decision": "decisions",
    "Decision": "decisions",
    "record_outcome": "outcome_evidence",
    "outcome_counts": "outcome_evidence",
    "OutcomeEvidence": "outcome_evidence",
}


def _callee(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return "<unknown>"


def _written_kinds() -> tuple[dict[str, str], list[str]]:
    """Literal `subject_kind=` writes aimed at `requirements`, plus any unclassified.

    Returns `(kinds_by_file, unclassified)`.
    """
    found: dict[str, str] = {}
    unclassified: list[str] = []
    for path in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not parseable, not our concern
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg != "subject_kind":
                    continue
                if not (isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)):
                    continue
                target = _TARGETS.get(_callee(node))
                if target is None:
                    unclassified.append(f"{path.name}:{_callee(node)}")
                elif target == "requirements":
                    found[kw.value.value] = path.name
    return found, unclassified


def test_every_subject_kind_the_code_writes_is_one_the_database_accepts() -> None:
    permitted = _permitted_kinds()
    written, unclassified = _written_kinds()

    assert written, "found no subject_kind writes at all -- the scan is broken, not the code"
    assert not unclassified, (
        f"these write a subject_kind and this test does not know which table they mean: "
        f"{sorted(set(unclassified))}. Add them to `_TARGETS`. Guessing would either "
        "resurrect a false positive or silently stop checking a real writer."
    )

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
    written, _ = _written_kinds()
    assert written.get("design") == "repository.py"
