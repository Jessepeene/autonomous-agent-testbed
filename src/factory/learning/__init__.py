"""The learning loop: prompts and scoring weights the factory may tune from its own
outcome data, within a hard, code-enforced boundary.

This package is the *only* place in the factory permitted to mutate a prompt or a
scoring weight at runtime, and only for the four keys in ``allowlist.MUTABLE_KEYS``.
The LIA, Bug Council, orchestrator, and codegen prompts never import anything from
here — that absence is the real enforcement; ``allowlist.assert_mutable`` is the
defense-in-depth backstop for every write path within this package itself.
"""

from __future__ import annotations
