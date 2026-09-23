"""Every idempotency key for a task kind known to recur must name the occurrence,
not just the entity.

Phase 2.13's first standing check, ARCHITECTURE_ROADMAP.md. Three real bugs, found
and fixed by hand, one at a time:

- BUG_LOG 38: `validate:{idea_id}` answered "has this idea ever been built" when the
  question was "is this build already queued" -- a decision to rebuild was silently
  dropped and read in the log as a deliberate pause.
- BUG_LOG 42: `build:{idea_id}` -- the same shape, one call site over.
- BUG_LOG 46: `evaluate-validation:{product_id}` -- a lifetime key on the one task
  kind whose whole point, after this company stopped auto-retiring underpowered
  validations, is to run again once more outreach goes out. The second real send
  would have queued nothing, silently, and the product would have sat at
  `VALIDATING` forever.

Three instances of the same shape, three separate discoveries. This is the standing
version: it would have failed on all three before they were fixed, and it fails on a
fourth wherever one is next written.
"""

from __future__ import annotations

import pathlib

from factory.selfcheck import find_lifetime_scoped_keys

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def test_no_recurring_task_kind_is_enqueued_under_a_lifetime_key() -> None:
    findings = find_lifetime_scoped_keys(_SRC)
    assert not findings, (
        "these enqueue a task kind known to recur under a key that does not name "
        "which occurrence this is -- add whatever identifies this specific attempt "
        "(a task id, an AB test id, a heal cycle bucket) to the key, the same fix "
        "BUG_LOG 38/42/46 already made three times over: "
        + ", ".join(f"{f.file}:{f.line} ({f.kind}: {f.key_source})" for f in findings)
    )


def test_the_scan_actually_finds_the_shape_it_exists_to_catch() -> None:
    """Pins the checker itself against a synthetic instance of exactly the bug, so a
    scan that silently stopped matching anything fails loudly rather than passing
    by accident forever."""
    import ast
    import tempfile

    from factory.selfcheck import find_lifetime_scoped_keys

    broken = (
        "from factory.settings import EngineName\n"
        "from factory.state.schemas import TaskKind\n\n"
        "async def _reproduce(session, product_id):\n"
        "    await repository.enqueue_task(\n"
        "        session,\n"
        "        kind=TaskKind.BUILD_MVP,\n"
        "        engine=EngineName.DEVELOPMENT,\n"
        "        idempotency_key=f'build:{product_id}',\n"
        "    )\n"
    )
    ast.parse(broken)  # fails loudly here, not inside a temp-file diff, if malformed

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "reproduce.py").write_text(broken, encoding="utf-8")
        findings = find_lifetime_scoped_keys(root)

    assert len(findings) == 1
    assert findings[0].kind == "BUILD_MVP"


def test_a_deliberately_once_only_kind_is_not_flagged() -> None:
    """`ASSESS_COMPLIANCE`'s ordinary key really is just `{idea_id}`, correctly --
    an idea is assessed for compliance once. The checker must tell "this kind
    recurs and was scoped wrong" apart from "this kind is meant to happen once",
    which is a curated fact about the kind, not something derivable from the key
    string alone."""
    import tempfile

    source = (
        "from factory.settings import EngineName\n"
        "from factory.state.schemas import TaskKind\n\n"
        "async def _approve(session, idea_id):\n"
        "    await repository.enqueue_task(\n"
        "        session,\n"
        "        kind=TaskKind.ASSESS_COMPLIANCE,\n"
        "        engine=EngineName.COMPLIANCE,\n"
        "        idempotency_key=f'compliance:idea:{idea_id}',\n"
        "    )\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "approve.py").write_text(source, encoding="utf-8")
        findings = find_lifetime_scoped_keys(root)
    assert findings == []
