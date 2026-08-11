"""Deterministic SEO hygiene check — the deploy gate's cheapest, earliest step.

Both codegen prompts (`SYSTEM_PROMPT`, `LANDING_PAGE_SYSTEM`) ask for a `<title>`, a
meta description, and a `sitemap.xml`/`robots.txt`. A prompt asking for something is
not the same as it being there — the same reasoning behind Bug Council's
`scan_secrets`/`scan_privilege`: a question with a checkable answer must not depend
on a model's mood.

Deliberately not part of `bug_council.convene()`'s unanimous-approval gate: an SEO
gap is exactly the narrow, targeted kind of fix the surgical repair path exists for
(add a missing tag, not regenerate the app), and unlike a Council rejection this
check is cheap to re-run after an edit — no LLM call, no risk of editing blind to an
opinion that can't be re-asked. Run *before* Bug Council, not after: catching a
missing `<title>` here costs nothing, where finding out about it only after paying
for three Opus reviewers would not.
"""

from __future__ import annotations

import re

_TITLE_RE = re.compile(r"<title>\s*\S.*?</title>", re.IGNORECASE | re.DOTALL)
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_DESCRIPTION_NAME_RE = re.compile(r"""name\s*=\s*["']description["']""", re.IGNORECASE)
_NONEMPTY_CONTENT_RE = re.compile(r"""content\s*=\s*["'][^"']*\S[^"']*["']""", re.IGNORECASE)


def _html_files(sources: dict[str, str]) -> dict[str, str]:
    return {path: body for path, body in sources.items() if path.lower().endswith(".html")}


def _has_meta_description(html: str) -> bool:
    for tag in _META_TAG_RE.findall(html):
        if _DESCRIPTION_NAME_RE.search(tag) and _NONEMPTY_CONTENT_RE.search(tag):
            return True
    return False


def missing_seo_requirements(sources: dict[str, str]) -> list[str]:
    """What a generated product is missing, or an empty list if nothing is.

    Deliberately narrow: the prompts also ask for Open Graph tags, a canonical URL,
    and semantic landmark elements, none of which are gated here. Those are real
    but harder to verify unambiguously by pattern match without false positives —
    the four checked here (title, meta description, sitemap, robots) are cheap and
    unambiguous. Skips entirely if no HTML file exists at all: both prompts already
    require a pytest suite that asserts the page loads, so a passing test run
    (a precondition for this check ever running -- see `_verify_and_ship`) already
    implies an HTML file exists; this is a defensive fallback, not the primary
    enforcement of that requirement.
    """
    html_files = _html_files(sources)
    if not html_files:
        return []

    missing: list[str] = []
    if not any(_TITLE_RE.search(body) for body in html_files.values()):
        missing.append("no non-empty <title> tag found in any HTML file")
    if not any(_has_meta_description(body) for body in html_files.values()):
        missing.append('no <meta name="description" content="..."> tag found in any HTML file')
    if not any(path.lower().endswith("sitemap.xml") for path in sources):
        missing.append("no sitemap.xml file")
    if not any(path.lower().endswith("robots.txt") for path in sources):
        missing.append("no robots.txt file")
    return missing
