"""The system checking itself for the shapes of bug it keeps finding by hand.

Phase 2.13's first deliverable (ARCHITECTURE_ROADMAP.md): detection before repair.
Every check here is read-only -- it reports findings and changes nothing, which is
deliberate. No write access, no rails at risk, and it attacks the half of this
project's bugs that were purely invisible rather than genuinely hard to reason about.

Each checker earns its place the same way: a real bug it would have caught, found and
fixed by hand first, calibrated against the codebase as it stands *after* the fix so it
starts clean rather than noisy. A checker that cannot point at a real historical
instance is a guess about what might go wrong, not a lesson this company already paid
for.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

#: Task kinds whose real-world work is meant to happen more than once for the same
#: product or idea -- a rebuild after a fixed defect, a repair after a resolved
#: blocker, a re-evaluation after more outreach. Curated, not inferred: whether a kind
#: recurs is a fact about what the work *is*, which no scan of the key string alone can
#: tell. `ASSESS_COMPLIANCE`'s ordinary key, deliberately just `{idea_id}`, is correct
#: for the opposite reason -- an idea is assessed once -- and would be a false positive
#: read as this shape.
RECURRING_KINDS: frozenset[str] = frozenset(
    {
        "BUILD_MVP",
        "BUILD_LANDING_PAGE",
        "EVALUATE_VALIDATION",
        "PATCH_PRODUCT",
    }
)

#: Below this many interpolated values, an idempotency key for a recurring kind is
#: scoped to an entity's whole lifetime rather than to one occurrence of its work.
#: This is the exact, mechanical shape of BUG_LOG 38, 42 and 46: each was a single
#: `{idea_id}` or `{product_id}` standing in for "this idea/product", answering "has
#: this ever happened" when the question the key exists to answer is "is this
#: happening now" -- and each was fixed by adding a second interpolation naming the
#: specific attempt (a task id, an AB test id, a heal cycle). Two is not a magic
#: number tuned to today's code; it is the smallest key that can name both "which
#: entity" and "which occurrence" at once, which is what every fix already did.
MIN_INTERPOLATIONS_FOR_RECURRING_KIND = 2


@dataclass(frozen=True, slots=True)
class LifetimeKeyFinding:
    file: str
    line: int
    kind: str
    key_source: str


def _kind_name(node: ast.expr) -> str | None:
    """`TaskKind.BUILD_MVP` -> `"BUILD_MVP"`. `None` for anything else, including a
    variable -- a kind chosen dynamically is not this checker's business; it can only
    read what is written in the call site itself."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        if node.value.id == "TaskKind":
            return node.attr
    return None


def _interpolation_count(node: ast.expr) -> int | None:
    """How many distinct values are interpolated into a key expression.

    Handles an f-string directly (the form every real call site in this codebase
    uses) and `str.format()`/`+`-concatenation only by falling back to `None` --
    "cannot tell" is reported as its own thing, never silently read as zero, because
    a key this checker cannot parse is not evidence the key is fine.
    """
    if isinstance(node, ast.JoinedStr):
        return sum(1 for part in node.values if isinstance(part, ast.FormattedValue))
    return None


def find_lifetime_scoped_keys(src_root: pathlib.Path) -> list[LifetimeKeyFinding]:
    """Every `enqueue_task(kind=<a recurring kind>, idempotency_key=...)` call whose
    key does not name a specific occurrence of that kind's work.

    Walks every `.py` file under `src_root` looking for a call to anything named
    `enqueue_task` (attribute or bare) -- deliberately not resolving imports to
    confirm it is *the* `repository.enqueue_task`, because a second function with
    that name and that shape of keyword arguments would deserve the same check, not
    an exemption for having a different origin.
    """
    findings: list[LifetimeKeyFinding] = []
    for path in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _callee_name(node) == "enqueue_task"):
                continue
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            kind_node = kwargs.get("kind")
            key_node = kwargs.get("idempotency_key")
            if kind_node is None or key_node is None:
                continue
            kind = _kind_name(kind_node)
            if kind not in RECURRING_KINDS:
                continue
            count = _interpolation_count(key_node)
            if count is not None and count < MIN_INTERPOLATIONS_FOR_RECURRING_KIND:
                findings.append(
                    LifetimeKeyFinding(
                        file=str(path.relative_to(src_root.parent)),
                        line=node.lineno,
                        kind=kind,
                        key_source=ast.unparse(key_node),
                    )
                )
    return findings


def _callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None
