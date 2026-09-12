"""Deterministic codegen contract check — the deploy gate's second-cheapest,
second-earliest step, right after `seo.py`.

Both codegen prompts (`SYSTEM_PROMPT`, `LANDING_PAGE_SYSTEM`) *ask* for several
load-bearing facts to be true: the real MVP must read its Stripe key from
`STRIPE_API_KEY` and its Checkout price from `PRODUCT_PRICE_CENTS`, and
attribute payments via `client_reference_id`; both prompts must wire the
analytics beacon to the product's own URL, and both must link a privacy page.
A prompt asking for something is not the same as it being there — same
reasoning as `seo.py`'s own module docstring: a question with a checkable
answer must not depend on a model's mood.

Deliberately regex/substring matching, not AST parsing, matching `seo.py`'s own
established precedent (see its docstring: checks are kept to what's cheap and
unambiguous by pattern match; anything that would need real parsing to avoid
false positives is left unchecked rather than attempted here). The generated
stack spans a Python backend and an HTML/JS frontend, so a genuinely robust AST
check would need two separate parsers for a risk this codebase's own
established philosophy already treats as disproportionate. The patterns below
are deliberately tighter than a bare substring test, though: they require an
actual *read* of the environment variable, not just its name appearing
anywhere (including inside a comment) — see `_PRICE_ENV_READ_RE`.

`is_validation` distinguishes the two build types this same gate runs for
(`development/graph.py::_verify_and_ship` is shared by both): a validation
landing page has no Stripe/PRODUCT_ID wiring at all, by design
(`LANDING_PAGE_SYSTEM`'s "This is deliberately NOT a real product" section), so
checking for `PRODUCT_PRICE_CENTS`/`client_reference_id` there would be a
false positive on every single validation build.
"""

from __future__ import annotations

import re


def _env_read_pattern(name: str) -> re.Pattern[str]:
    """Requires an actual read of `name`, not just the bare string appearing
    anywhere (including inside a comment) -- a `# TODO: read X` must not
    satisfy this. Covers all three idioms Python code actually reads an
    environment variable with: `os.environ["X"]`, `os.getenv("X")`, and
    `os.environ.get("X")`.

    Real bug, found live: the original pattern only covered the first two.
    `os.environ.get(...)` -- an equally common, equally correct idiom, and
    the one the model actually used -- silently never matched, so
    `missing_contract_requirements` false-positived on genuinely correct
    code. This sent a product whose Stripe key *and* price were both wired
    correctly into an unnecessary heal cycle, which then exhausted the
    autonomous ceiling and escalated to a human over a bug that was never
    actually there -- precisely the class of wasted escalation this
    deterministic gate exists to prevent, not cause.
    """
    escaped = re.escape(name)
    return re.compile(
        rf"""os\.environ\[\s*["']{escaped}["']\s*\]"""
        rf"""|os\.getenv\(\s*["']{escaped}["']"""
        rf"""|os\.environ\.get\(\s*["']{escaped}["']"""
    )


_PRICE_ENV_READ_RE = _env_read_pattern("PRODUCT_PRICE_CENTS")
#: Added after a real, repeated live failure: `SYSTEM_PROMPT` never used to
#: name the Stripe key's exact environment variable (just "reading the key
#: from an environment variable"), and the model consistently guessed
#: `STRIPE_SECRET_KEY` -- the name Stripe's own docs use -- instead of
#: `STRIPE_API_KEY`, the only name `deploy.py::_runtime_env` ever actually
#: sets. Four separate products hit this identically: the key read as empty
#: at runtime, so billing failed closed on every checkout attempt. The
#: prompt now names it explicitly too; this check exists so a regression is
#: caught here, cheaply, before ever reaching Bug Council.
_STRIPE_KEY_ENV_READ_RE = _env_read_pattern("STRIPE_API_KEY")
_CLIENT_REFERENCE_RE = re.compile(r"client_reference_id")
_PRIVACY_LINK_RE = re.compile(r"""<a\b[^>]*href\s*=\s*["'][^"']*privacy[^"']*["']""", re.IGNORECASE)


def _python_files(sources: dict[str, str]) -> dict[str, str]:
    return {path: body for path, body in sources.items() if path.lower().endswith(".py")}


def _html_files(sources: dict[str, str]) -> dict[str, str]:
    return {path: body for path, body in sources.items() if path.lower().endswith(".html")}


def missing_contract_requirements(
    sources: dict[str, str], *, product_id: str, is_validation: bool
) -> list[str]:
    """What a generated product's codegen contract is missing, or an empty list if
    nothing is. See the module docstring for why `is_validation` changes which
    facts are even checkable."""
    missing: list[str] = []
    html_files = _html_files(sources)
    python_files = _python_files(sources)

    # Defensive skip, same reasoning as seo.py's "no HTML files at all" case: a
    # build that reached this gate already passed its test suite, which already
    # implies real backend files exist -- a workspace with literally zero .py
    # files is a different, earlier problem this check isn't meant to catch, not
    # something to flag on top of it.
    if not is_validation and python_files:
        python_blob = "\n".join(python_files.values())
        if not _PRICE_ENV_READ_RE.search(python_blob):
            missing.append(
                "PRODUCT_PRICE_CENTS is never read from the environment anywhere in "
                "the backend -- the Checkout amount must come from it, never be "
                "hard-coded"
            )
        if not _STRIPE_KEY_ENV_READ_RE.search(python_blob):
            missing.append(
                "STRIPE_API_KEY is never read from the environment anywhere in the "
                "backend -- that exact name, not STRIPE_SECRET_KEY or any other "
                "guess, is the only one the deploy pipeline actually sets, so "
                "billing silently fails closed without it"
            )
        if not _CLIENT_REFERENCE_RE.search(python_blob):
            missing.append(
                "client_reference_id is never referenced anywhere in the backend -- "
                "a payment cannot be attributed back to this product without it"
            )

    beacon_path = f"/analytics/{product_id}"
    if html_files and not any(beacon_path in body for body in html_files.values()):
        missing.append(f"no reference to the analytics beacon URL ({beacon_path}) in any HTML file")

    if is_validation:
        interest_path = f"/interest/{product_id}"
        if html_files and not any(interest_path in body for body in html_files.values()):
            missing.append(
                f"no reference to the interest-capture URL ({interest_path}) in any HTML file"
            )

    if html_files:
        has_privacy_page = any("privacy" in path.lower() for path in html_files)
        if not has_privacy_page:
            missing.append("no privacy.html (or similarly named) page found")
        elif not any(_PRIVACY_LINK_RE.search(body) for body in html_files.values()):
            missing.append("a privacy page exists but is not linked from any page")

    return missing
