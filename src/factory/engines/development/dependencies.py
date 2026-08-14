"""Deterministic slopsquatting defense — the deploy gate's other cheap, early check.

Bug Council's own dependency reviewer (`bug_council.py`) is already asked to flag
"packages that are abandoned or typosquat a popular name", but that is a Claude
call reasoning about plausibility, not a verified fact. A hallucinated package name
that *sounds* plausible to a model can sound just as plausible to the reviewer
grading it — the same reasoning behind `seo.py`'s module docstring and Bug
Council's own `scan_secrets`/`scan_privilege`: a question with a checkable answer
must not depend on a model's mood. "Does this name exist on PyPI, and if so, how
long has it existed" are both checkable.

Run *before* `pip install`, not after: Bug Council's dependency reviewer runs only
once a build has already installed cleanly and passed its tests (see
`_verify_and_ship`), so it never actually sees a package that failed to install —
by the time a hallucinated name would reach it, the install already failed with a
generic `dependency_failure` and the build regenerated from scratch. Checking here
first, before any network call the sandbox itself would make, catches it while it
is still a one-line, surgically-repairable edit to `requirements.txt`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from factory.logging import get_logger
from factory.settings import Settings
from factory.state import repository

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_USER_AGENT = "superagent-factory/0.1 (+dependency verification; contact via source site)"

#: How long a cached verdict is trusted before this checks PyPI again. Short enough
#: that a genuinely new (but real) package stops being flagged within a week of the
#: threshold passing; long enough that a build a minute after the last one doesn't
#: re-hit PyPI for the same name.
_CACHE_TTL = timedelta(days=7)

#: A requirements.txt line's package-name portion, before any version specifier,
#: extra, or environment marker. Deliberately does not try to be a full PEP 508
#: parser -- the name is all this check needs, and a name it can't confidently
#: extract is skipped rather than guessed at (see parse_requirements).
_NAME_RE = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")

#: PEP 503: normalize by lowercasing and collapsing runs of -_. into a single -.
_NORMALIZE_RE = re.compile(r"[-_.]+")


def _normalize(name: str) -> str:
    return _NORMALIZE_RE.sub("-", name).lower()


def parse_requirements(text: str) -> list[str]:
    """Extract normalized package names from a requirements.txt body.

    Skips: blank lines, comments, ``-r``/``-e``/``--`` option lines (nothing to
    verify against PyPI -- a ``-r`` include or an editable/local install has no
    registry name at all), and anything that looks like a direct URL or VCS
    reference (``://`` or a ``+`` scheme prefix), since those name a source, not
    a PyPI project. A line this can't confidently extract a name from is
    skipped, not guessed at -- a false negative here just means one less name
    checked, where a false positive would flag a real, legitimate dependency.
    """
    names: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Every PEP 508 direct-reference form (a plain URL, or a VCS reference
        # like `git+https://...`) contains "://" -- catching that one substring
        # covers both without needing to enumerate VCS scheme prefixes. A local
        # path (`./pkg`, `../pkg`) doesn't match _NAME_RE at all below, since it
        # doesn't start with an alphanumeric character.
        if "://" in line:
            continue
        match = _NAME_RE.match(line)
        if match is None:
            continue
        names.append(_normalize(match.group(1)))
    return names


def _earliest_release(releases: dict[str, Any]) -> datetime | None:
    timestamps: list[datetime] = []
    for files in releases.values():
        if not isinstance(files, list):
            continue
        for entry in files:
            raw = entry.get("upload_time_iso_8601") if isinstance(entry, dict) else None
            if not isinstance(raw, str):
                continue
            try:
                timestamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                continue
    return min(timestamps) if timestamps else None


async def _check_one(client: httpx.AsyncClient, name: str) -> tuple[bool, datetime | None] | None:
    """``(exists, first_release_at)``, or ``None`` on any transport failure --
    fails open, same posture as ``website.py``'s ``_robots_allowed``: a PyPI
    outage must not block every build in progress, and this check has a second,
    later backstop anyway (the install itself fails if the name really is bad).
    """
    try:
        response = await client.get(f"https://pypi.org/pypi/{name}/json")
    except httpx.HTTPError as exc:
        log.warning("dependencies.pypi_unreachable", name=name, detail=str(exc)[:200])
        return None
    if response.status_code == 404:
        return False, None
    if response.status_code != 200:
        log.warning("dependencies.pypi_unexpected_status", name=name, status=response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    releases = payload.get("releases") if isinstance(payload, dict) else None
    first_release = _earliest_release(releases) if isinstance(releases, dict) else None
    return True, first_release


async def unresolvable_requirements(
    session: AsyncSession, text: str, settings: Settings
) -> list[str]:
    """Names from ``text`` that are either not real PyPI packages, or real but
    registered suspiciously recently -- either is worth a human-reviewable heal
    cycle rather than a silent install.

    Each name is checked at most once per `_CACHE_TTL` window regardless of how
    many builds reference it -- package existence and registration date are
    facts about the package, not about any one build.
    """
    if not settings.pypi_check_enabled:
        return []
    names = parse_requirements(text)
    if not names:
        return []

    findings: list[str] = []
    now = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=_TIMEOUT, headers={"User-Agent": _USER_AGENT}) as client:
        for name in names:
            cached = await repository.get_pypi_package_check(session, name)
            if cached is not None and (now - cached.checked_at) < _CACHE_TTL:
                exists, first_release_at = cached.exists, cached.first_release_at
            else:
                checked = await _check_one(client, name)
                if checked is None:
                    continue
                exists, first_release_at = checked
                await repository.upsert_pypi_package_check(
                    session, name=name, exists=exists, first_release_at=first_release_at
                )

            if not exists:
                findings.append(f"{name} (no such package on PyPI)")
                continue
            if first_release_at is None:
                continue
            age_days = (now - first_release_at).days
            if age_days < settings.pypi_min_package_age_days:
                findings.append(f"{name} (first published {age_days} day(s) ago)")
    return findings
