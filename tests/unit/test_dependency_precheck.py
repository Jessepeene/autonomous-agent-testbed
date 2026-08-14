"""`engines/development/dependencies.py` -- the deterministic slopsquatting
defense: does a requirements.txt name a real, sufficiently-aged PyPI package.

Unit-level only. `parse_requirements` is pure and tested directly; everything
that touches PyPI or the cache table is exercised with a mocked
`httpx.AsyncClient.get` and a hand-rolled fake session -- a real cache-TTL
round trip against Postgres belongs in integration coverage, not here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from factory.engines.development.dependencies import (
    parse_requirements,
    unresolvable_requirements,
)
from factory.settings import Settings

_REQUEST = httpx.Request("GET", "https://pypi.org/pypi/example/json")


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --- parse_requirements ---------------------------------------------------------


def test_parses_plain_names_with_version_specifiers() -> None:
    text = "pydantic>=2.9,<3\nrequests==2.31.0\nfastapi\n"
    assert parse_requirements(text) == ["pydantic", "requests", "fastapi"]


def test_normalizes_per_pep_503() -> None:
    """Case-insensitive, and runs of -_. all collapse to a single -, so
    `Django_Rest.Framework` and `django-rest-framework` are the same cache key."""
    assert parse_requirements("Django_Rest.Framework\n") == ["django-rest-framework"]


def test_skips_comments_and_blank_lines() -> None:
    text = "# a comment\n\npydantic>=2.9\n   \n# another\nrequests\n"
    assert parse_requirements(text) == ["pydantic", "requests"]


def test_skips_option_lines() -> None:
    text = "-r base.txt\n-e .\n--index-url https://example.test/simple\npydantic\n"
    assert parse_requirements(text) == ["pydantic"]


def test_skips_direct_urls_and_vcs_references() -> None:
    text = (
        "https://example.test/some-package.whl\ngit+https://github.com/example/repo.git\npydantic\n"
    )
    assert parse_requirements(text) == ["pydantic"]


def test_skips_local_paths() -> None:
    text = "./local-package\n../sibling-package\npydantic\n"
    assert parse_requirements(text) == ["pydantic"]


def test_extras_and_markers_do_not_pollute_the_name() -> None:
    text = 'pydantic[email]>=2.9; python_version>="3.11"\n'
    assert parse_requirements(text) == ["pydantic"]


def test_empty_requirements_file() -> None:
    assert parse_requirements("") == []
    assert parse_requirements("   \n\n") == []


# --- unresolvable_requirements ---------------------------------------------------


@dataclass
class _FakeCacheRow:
    name: str
    exists: bool
    first_release_at: datetime | None
    checked_at: datetime


@dataclass
class _FakeSession:
    """Stands in for the real repository functions -- a plain in-memory dict is
    enough to prove the caching/TTL logic without a database."""

    rows: dict[str, _FakeCacheRow] = field(default_factory=dict)


@pytest.fixture(autouse=True)
def _patch_repository(monkeypatch: pytest.MonkeyPatch) -> _FakeSession:
    fake = _FakeSession()

    async def fake_get(session: Any, name: str) -> _FakeCacheRow | None:
        return fake.rows.get(name)

    async def fake_upsert(
        session: Any, *, name: str, exists: bool, first_release_at: datetime | None
    ) -> None:
        fake.rows[name] = _FakeCacheRow(
            name=name,
            exists=exists,
            first_release_at=first_release_at,
            checked_at=datetime.now(UTC),
        )

    from factory.engines.development import dependencies as dependencies_module

    monkeypatch.setattr(dependencies_module.repository, "get_pypi_package_check", fake_get)
    monkeypatch.setattr(dependencies_module.repository, "upsert_pypi_package_check", fake_upsert)
    return fake


def _pypi_response(*, exists: bool, releases: dict[str, Any] | None = None) -> httpx.Response:
    if not exists:
        return httpx.Response(404, request=_REQUEST)
    return httpx.Response(200, json={"releases": releases or {}}, request=_REQUEST)


async def test_a_real_old_package_is_not_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    old_date = (datetime.now(UTC) - timedelta(days=3000)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return _pypi_response(exists=True, releases={"1.0.0": [{"upload_time_iso_8601": old_date}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await unresolvable_requirements(None, "requests>=2.31\n", _settings())  # type: ignore[arg-type]
    assert result == []


async def test_a_nonexistent_package_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return _pypi_response(exists=False)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await unresolvable_requirements(None, "totally-hallucinated-pkg\n", _settings())  # type: ignore[arg-type]
    assert len(result) == 1
    assert "no such package" in result[0]


async def test_a_freshly_registered_package_is_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual slopsquatting scenario: the attacker only just registered the
    hallucinated name after seeing it get predicted -- it exists, but its
    existence is itself the red flag."""
    recent = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return _pypi_response(exists=True, releases={"0.0.1": [{"upload_time_iso_8601": recent}]})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await unresolvable_requirements(  # type: ignore[arg-type]
        None, "react-codeshift\n", _settings(pypi_min_package_age_days=90)
    )
    assert len(result) == 1
    assert "2 day" in result[0]


async def test_a_transport_failure_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A PyPI outage must never block every build in flight -- same posture as
    `website.py`'s robots.txt fetch. The install step is the real backstop if
    a name really is bad; this check failing open just means it didn't get an
    early answer this time."""

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=_REQUEST)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await unresolvable_requirements(None, "pydantic>=2.9\n", _settings())  # type: ignore[arg-type]
    assert result == []


async def test_a_cache_hit_never_touches_the_network(
    monkeypatch: pytest.MonkeyPatch, _patch_repository: _FakeSession
) -> None:
    calls = 0

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _pypi_response(exists=False)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    _patch_repository.rows["pydantic"] = _FakeCacheRow(
        name="pydantic", exists=True, first_release_at=None, checked_at=datetime.now(UTC)
    )

    result = await unresolvable_requirements(None, "pydantic>=2.9\n", _settings())  # type: ignore[arg-type]

    assert result == []
    assert calls == 0


async def test_an_expired_cache_entry_is_refreshed(
    monkeypatch: pytest.MonkeyPatch, _patch_repository: _FakeSession
) -> None:
    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return _pypi_response(exists=False)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    _patch_repository.rows["stale-pkg"] = _FakeCacheRow(
        name="stale-pkg",
        exists=True,
        first_release_at=None,
        checked_at=datetime.now(UTC) - timedelta(days=30),
    )

    result = await unresolvable_requirements(None, "stale-pkg\n", _settings())  # type: ignore[arg-type]

    assert len(result) == 1
    assert "no such package" in result[0]


async def test_disabled_via_settings_skips_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        nonlocal called
        called = True
        return _pypi_response(exists=False)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await unresolvable_requirements(  # type: ignore[arg-type]
        None, "totally-hallucinated-pkg\n", _settings(pypi_check_enabled=False)
    )
    assert result == []
    assert called is False


async def test_no_requirements_at_all_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        nonlocal called
        called = True
        return _pypi_response(exists=False)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    result = await unresolvable_requirements(None, "# nothing here\n", _settings())  # type: ignore[arg-type]
    assert result == []
    assert called is False
