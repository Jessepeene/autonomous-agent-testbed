"""Deterministic codegen contract check.

Both codegen prompts ask for several load-bearing facts to be true --
PRODUCT_PRICE_CENTS actually read, client_reference_id actually set, the
analytics/interest beacon wired to the right URL, a privacy page linked. A
prompt asking for something is not the same as it being there. Same
reasoning as `seo.py`'s own tests.
"""

from __future__ import annotations

import pytest

from factory.engines.development.contract import _env_read_pattern, missing_contract_requirements

_PRODUCT_ID = "prod-1"


# --- the shared env-read pattern, all three real Python idioms -------------------


@pytest.mark.parametrize(
    "snippet",
    [
        'os.environ["X"]',
        "os.environ['X']",
        'os.getenv("X")',
        'os.getenv("X", "default")',
        'os.environ.get("X")',
        'os.environ.get("X", "default")',
    ],
)
def test_env_read_pattern_matches_every_real_python_idiom(snippet: str) -> None:
    """Real bug, found live: the pattern originally covered only the first two
    idioms. `os.environ.get(...)` -- what the model actually wrote -- silently
    never matched, false-positiving a fully correct product into an
    unnecessary heal cycle and a real human escalation."""
    assert _env_read_pattern("X").search(snippet)


def test_env_read_pattern_does_not_match_a_bare_mention() -> None:
    assert not _env_read_pattern("X").search("# TODO: read X from the environment")


_GOOD_BACKEND_PY = (
    "import os\n"
    "def create_checkout():\n"
    "    stripe.api_key = os.environ['STRIPE_API_KEY']\n"
    "    price = int(os.environ['PRODUCT_PRICE_CENTS'])\n"
    "    return stripe.checkout.Session.create(\n"
    "        client_reference_id=os.environ['PRODUCT_ID'],\n"
    "        line_items=[{'price_data': {'unit_amount': price}}],\n"
    "    )\n"
)

_GOOD_HTML = (
    "<html><body>"
    '<script>fetch("https://example.test/analytics/prod-1")</script>'
    '<a href="privacy.html">Privacy</a>'
    "</body></html>"
)

_GOOD_REAL_MVP_SOURCES = {
    "app/main.py": _GOOD_BACKEND_PY,
    "app/index.html": _GOOD_HTML,
    "app/privacy.html": "<html><body>We store your account email.</body></html>",
}


def _missing(sources: dict[str, str], *, is_validation: bool = False) -> list[str]:
    return missing_contract_requirements(
        sources, product_id=_PRODUCT_ID, is_validation=is_validation
    )


# --- real MVP mode --------------------------------------------------------------


def test_a_fully_compliant_real_mvp_has_nothing_missing() -> None:
    assert _missing(_GOOD_REAL_MVP_SOURCES) == []


def test_a_missing_price_env_read_is_flagged() -> None:
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/main.py"] = _GOOD_BACKEND_PY.replace(
        "int(os.environ['PRODUCT_PRICE_CENTS'])", "499"
    )
    missing = _missing(sources)
    assert any("PRODUCT_PRICE_CENTS" in item for item in missing)


def test_a_bare_getenv_call_also_satisfies_the_price_check() -> None:
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/main.py"] = _GOOD_BACKEND_PY.replace(
        "os.environ['PRODUCT_PRICE_CENTS']", 'os.getenv("PRODUCT_PRICE_CENTS")'
    )
    missing = _missing(sources)
    assert not any("PRODUCT_PRICE_CENTS" in item for item in missing)


def test_an_os_environ_get_call_also_satisfies_the_price_check() -> None:
    """Real bug, found live: the original regex only covered `os.environ[...]`
    and `os.getenv(...)`, missing the equally common, equally correct
    `os.environ.get(...)` idiom entirely -- which is what the model actually
    wrote. A product whose Stripe key and price were both wired correctly
    got a false-positive `contract_incomplete` rejection, burned an
    unnecessary heal cycle on it, and escalated to a human over a bug that
    was never there."""
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/main.py"] = _GOOD_BACKEND_PY.replace(
        "os.environ['PRODUCT_PRICE_CENTS']", 'os.environ.get("PRODUCT_PRICE_CENTS")'
    )
    missing = _missing(sources)
    assert not any("PRODUCT_PRICE_CENTS" in item for item in missing)


def test_the_env_var_name_inside_a_comment_does_not_satisfy_the_check() -> None:
    """The whole point of requiring an actual read pattern, not a bare substring
    match: a model that merely left a TODO must still be caught."""
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/main.py"] = "# TODO: read PRODUCT_PRICE_CENTS from the environment\n"
    missing = _missing(sources)
    assert any("PRODUCT_PRICE_CENTS" in item for item in missing)


def test_a_missing_stripe_api_key_read_is_flagged() -> None:
    """Real bug, found live: four separate products read STRIPE_SECRET_KEY (the
    name Stripe's own docs use) instead of STRIPE_API_KEY, the only name the
    deploy pipeline actually sets -- billing failed closed every time, four
    times, before this was ever caught."""
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/main.py"] = _GOOD_BACKEND_PY.replace(
        "os.environ['STRIPE_API_KEY']", "os.environ['STRIPE_SECRET_KEY']"
    )
    missing = _missing(sources)
    assert any("STRIPE_API_KEY" in item for item in missing)


def test_a_bare_getenv_call_also_satisfies_the_stripe_key_check() -> None:
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/main.py"] = _GOOD_BACKEND_PY.replace(
        "os.environ['STRIPE_API_KEY']", 'os.getenv("STRIPE_API_KEY")'
    )
    missing = _missing(sources)
    assert not any("STRIPE_API_KEY" in item for item in missing)


def test_an_os_environ_get_call_also_satisfies_the_stripe_key_check() -> None:
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/main.py"] = _GOOD_BACKEND_PY.replace(
        "os.environ['STRIPE_API_KEY']", 'os.environ.get("STRIPE_API_KEY")'
    )
    missing = _missing(sources)
    assert not any("STRIPE_API_KEY" in item for item in missing)


def test_a_missing_client_reference_id_is_flagged() -> None:
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/main.py"] = _GOOD_BACKEND_PY.replace(
        "client_reference_id=os.environ['PRODUCT_ID'],\n", ""
    )
    missing = _missing(sources)
    assert any("client_reference_id" in item for item in missing)


def test_a_missing_analytics_beacon_reference_is_flagged() -> None:
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/index.html"] = _GOOD_HTML.replace(
        'fetch("https://example.test/analytics/prod-1")', ""
    )
    missing = _missing(sources)
    assert any("analytics" in item for item in missing)


def test_the_beacon_check_is_scoped_to_this_products_own_url() -> None:
    """A generic '/analytics/' substring must not satisfy this -- it has to be
    THIS product's own beacon path, or telemetry silently goes to the wrong
    product (or nowhere)."""
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/index.html"] = _GOOD_HTML.replace(
        "/analytics/prod-1", "/analytics/some-other-product"
    )
    missing = _missing(sources)
    assert any("analytics" in item for item in missing)


# --- privacy page ----------------------------------------------------------------


def test_a_missing_privacy_page_is_flagged() -> None:
    sources = {k: v for k, v in _GOOD_REAL_MVP_SOURCES.items() if k != "app/privacy.html"}
    missing = _missing(sources)
    assert any("privacy" in item for item in missing)


def test_a_privacy_page_that_exists_but_is_unlinked_is_flagged() -> None:
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/index.html"] = _GOOD_HTML.replace('<a href="privacy.html">Privacy</a>', "")
    missing = _missing(sources)
    assert any("not linked" in item for item in missing)


def test_a_stray_mention_of_the_word_privacy_does_not_count_as_a_link() -> None:
    sources = dict(_GOOD_REAL_MVP_SOURCES)
    sources["app/index.html"] = _GOOD_HTML.replace(
        '<a href="privacy.html">Privacy</a>', "<p>See our privacy practices.</p>"
    )
    missing = _missing(sources)
    assert any("not linked" in item for item in missing)


# --- validation (landing page) mode -----------------------------------------------


def _good_validation_sources() -> dict[str, str]:
    html = (
        "<html><body>"
        '<form action="/interest/prod-1"></form>'
        '<script>fetch("https://example.test/interest/prod-1");'
        'fetch("https://example.test/analytics/prod-1")</script>'
        '<a href="privacy.html">Privacy</a>'
        "</body></html>"
    )
    return {
        "app/index.html": html,
        "app/privacy.html": "<html><body>We only use your email to notify you.</body></html>",
    }


def test_a_fully_compliant_validation_page_has_nothing_missing() -> None:
    assert _missing(_good_validation_sources(), is_validation=True) == []


def test_validation_mode_never_checks_for_stripe_wiring() -> None:
    """LANDING_PAGE_SYSTEM explicitly forbids Stripe/billing on a validation page
    -- checking for PRODUCT_PRICE_CENTS/client_reference_id here would flag every
    single validation build for something it must never have."""
    sources = _good_validation_sources()  # no .py files, no Stripe wiring at all
    missing = _missing(sources, is_validation=True)
    assert not any("PRODUCT_PRICE_CENTS" in item for item in missing)
    assert not any("client_reference_id" in item for item in missing)


def test_a_missing_interest_url_reference_is_flagged_in_validation_mode() -> None:
    sources = _good_validation_sources()
    # Strip every reference to the interest path, not just the fetch() call --
    # the <form action="..."> in _good_validation_sources also names it.
    sources["app/index.html"] = sources["app/index.html"].replace("/interest/prod-1", "")
    missing = _missing(sources, is_validation=True)
    assert any("interest" in item for item in missing)


def test_real_mvp_mode_never_requires_an_interest_url() -> None:
    """The interest-capture URL only exists for the validation flow -- a real
    MVP's own signup/checkout form must not be flagged for lacking it."""
    assert not any("interest" in item for item in _missing(_GOOD_REAL_MVP_SOURCES))


# --- defensive skip, matching seo.py's precedent ----------------------------------


def test_a_workspace_with_no_html_files_skips_html_only_checks() -> None:
    sources = {"app/main.py": _GOOD_BACKEND_PY}
    missing = _missing(sources)
    assert not any("analytics" in item or "privacy" in item for item in missing)


def test_multiple_gaps_are_all_reported_at_once() -> None:
    missing = _missing({"app/main.py": "print('nothing here')"})
    # PRODUCT_PRICE_CENTS + STRIPE_API_KEY + client_reference_id; no HTML files
    # at all, so the beacon/privacy checks are skipped rather than also flagged.
    assert len(missing) == 3
