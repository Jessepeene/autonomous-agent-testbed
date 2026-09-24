"""Change the objective. Run by a human, directly, on purpose.

The only writer of the `objectives` table (Phase 5a, `ARCHITECTURE_ROADMAP.md`).
There is no `TaskKind`, no agent tool, and no engine call site for this -- an
autonomous system able to edit the one row that defines what it exists to do would
make "immutable objective" a decoration rather than a property. If you are looking
for a way to let an agent update this, the roadmap's answer is: don't.

Usage:

    python scripts/set_objective.py --show
    python scripts/set_objective.py --statement "..." --set-by jesse --reason "..." --confirm

`--show` prints the current live objective (or that none is set) and exits --
always run this first. Setting a new one without `--confirm` prints exactly what
would change and does nothing else, the same shape as every other confirm-to-act
control in this codebase (`git commit --amend` without `-m`, a `--dry-run` flag);
`--confirm` is what actually supersedes the current row.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from factory.state import repository
from factory.state.db import session_scope


async def _show() -> None:
    async with session_scope() as session:
        current = await repository.live_objective(session)
    if current is None:
        print("No objective has ever been set.")
        return
    print(f"Live since : {current.created_at.isoformat()}")
    print(f"Set by     : {current.set_by}")
    if current.reason:
        print(f"Reason     : {current.reason}")
    print("Statement  :")
    print(current.statement)


async def _set(statement: str, set_by: str, reason: str | None, confirm: bool) -> None:
    async with session_scope() as session:
        current = await repository.live_objective(session)

    print("=== Current ===")
    print(current.statement if current else "(none set)")
    print()
    print("=== Proposed ===")
    print(statement)
    print()
    print(f"set_by={set_by!r} reason={reason!r}")

    if not confirm:
        print()
        print("Nothing written. Re-run with --confirm to supersede the current objective.")
        return

    async with session_scope() as session:
        new_id = await repository.record_objective(
            session, statement=statement, set_by=set_by, reason=reason
        )
    print()
    print(f"Written. New objective id: {new_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="Print the current objective and exit.")
    parser.add_argument("--statement", help="The new objective, in full.")
    parser.add_argument("--set-by", help="Who is setting it (a name, not a role).")
    parser.add_argument("--reason", default=None, help="Why it is changing.")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually write the change. Without this, only a preview is printed.",
    )
    args = parser.parse_args()

    if args.show:
        asyncio.run(_show())
        return 0

    if not args.statement or not args.set_by:
        parser.error("--statement and --set-by are required unless --show is given.")

    asyncio.run(_set(args.statement, args.set_by, args.reason, args.confirm))
    return 0


if __name__ == "__main__":
    sys.exit(main())
