"""The browser tools' guards -- origin pinning and the call-site role scope.

`http_get` has no SSRF guard at all: its only check is the scheme prefix, so
`http://169.254.169.254/` and `http://postgres:5432/` are both reachable from it
today. A real browser is strictly worse in that respect -- it follows subresource
requests and JS-initiated navigation, and it runs inside the compose network -- so
the reviewer gets exactly one origin: the product's own.

None of these tests launch a browser. The origin guard runs before Playwright is
ever touched, which is the point: a refusal must not depend on the optional extra
being installed.
"""

from __future__ import annotations

import pytest

from factory.agents.roles import CALL_SITE_SCOPED_ROLES, RoleName, role_catalogue
from factory.tools.browser import BrowserSession, allowed_origins_for, origin_of


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.test/path?q=1", "https://example.test"),
        ("http://Example.TEST:8080/x", "http://example.test:8080"),
        ("https://sub.example.test", "https://sub.example.test"),
    ],
)
def test_origin_of_extracts_scheme_host_port(url: str, expected: str) -> None:
    assert origin_of(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.test/x",
        "javascript:alert(1)",
        "not-a-url",
        "",
        "https://",  # scheme but no host
    ],
)
def test_origin_of_rejects_anything_that_is_not_absolute_http(url: str) -> None:
    assert origin_of(url) is None


def test_allowed_origins_collects_deploy_url_and_custom_domain() -> None:
    origins = allowed_origins_for("https://p.vercel.app/", "https://real.example.test")
    assert origins == {"https://p.vercel.app", "https://real.example.test"}


def test_allowed_origins_drops_missing_and_unparseable_entries() -> None:
    """A product with no custom domain is the normal case, not an error."""
    assert allowed_origins_for("https://p.vercel.app", None) == {"https://p.vercel.app"}
    assert allowed_origins_for(None, "") == set()


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://postgres:5432/",  # a sibling compose service
        "http://localhost:8000/dashboard",  # the factory's own API
        "https://evil.test/",
        "file:///etc/passwd",
    ],
)
def test_navigation_off_the_allowed_origin_is_refused(url: str) -> None:
    """The refusal is computed before Playwright is imported, so it holds even
    where the optional browser extra is not installed."""
    session = BrowserSession(allowed_origins={"https://p.vercel.app"})
    refusal = session._refuse_origin(url)
    assert refusal is not None
    assert "error" in refusal


def test_navigation_to_the_products_own_origin_is_allowed() -> None:
    session = BrowserSession(allowed_origins={"https://p.vercel.app"})
    assert session._refuse_origin("https://p.vercel.app/pricing") is None


def test_auditor_is_hidden_from_the_orchestrators_role_menu() -> None:
    """Its tools only exist in a private per-run registry; reached through a
    generic spawn it would run with no tools at all."""
    assert RoleName.AUDITOR in CALL_SITE_SCOPED_ROLES
    assert "auditor" not in role_catalogue()
