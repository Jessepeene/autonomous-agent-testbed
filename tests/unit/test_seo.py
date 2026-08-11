"""Deterministic SEO hygiene check.

Both codegen prompts ask for a <title>, a meta description, and sitemap.xml/
robots.txt -- a prompt asking for something is not the same as it being there.
Same reasoning as Bug Council's scan_secrets/scan_privilege: a question with a
checkable answer must not depend on a model's mood.
"""

from __future__ import annotations

from factory.engines.development.seo import missing_seo_requirements

_GOOD_HTML = """<!DOCTYPE html>
<html>
<head>
<title>A real product</title>
<meta name="description" content="Reconciles invoices automatically.">
</head>
<body><main><h1>Hi</h1></main></body>
</html>
"""

_GOOD_SOURCES = {
    "app/index.html": _GOOD_HTML,
    "app/sitemap.xml": "<urlset></urlset>",
    "app/robots.txt": "User-agent: *\nAllow: /\n",
}


def test_a_fully_compliant_page_has_nothing_missing() -> None:
    assert missing_seo_requirements(_GOOD_SOURCES) == []


def test_a_missing_title_is_flagged() -> None:
    sources = dict(_GOOD_SOURCES)
    sources["app/index.html"] = _GOOD_HTML.replace("<title>A real product</title>", "")
    missing = missing_seo_requirements(sources)
    assert any("title" in item for item in missing)


def test_an_empty_title_is_flagged() -> None:
    """<title></title> technically exists as a tag but conveys nothing -- the
    prompt asks for a *non-empty* title, and the check must actually enforce that,
    not just check for the tag's presence."""
    sources = dict(_GOOD_SOURCES)
    sources["app/index.html"] = _GOOD_HTML.replace(
        "<title>A real product</title>", "<title></title>"
    )
    missing = missing_seo_requirements(sources)
    assert any("title" in item for item in missing)


def test_a_missing_meta_description_is_flagged() -> None:
    sources = dict(_GOOD_SOURCES)
    sources["app/index.html"] = _GOOD_HTML.replace(
        '<meta name="description" content="Reconciles invoices automatically.">', ""
    )
    missing = missing_seo_requirements(sources)
    assert any("description" in item for item in missing)


def test_an_empty_meta_description_content_is_flagged() -> None:
    sources = dict(_GOOD_SOURCES)
    sources["app/index.html"] = _GOOD_HTML.replace(
        'content="Reconciles invoices automatically."', 'content=""'
    )
    missing = missing_seo_requirements(sources)
    assert any("description" in item for item in missing)


def test_meta_description_is_recognised_regardless_of_attribute_order() -> None:
    """HTML attribute order is not meaningful -- content= before name= must be
    recognised exactly like name= before content=, or a genuinely valid page could
    be rejected on a technicality the prompt never specified."""
    sources = dict(_GOOD_SOURCES)
    sources["app/index.html"] = _GOOD_HTML.replace(
        '<meta name="description" content="Reconciles invoices automatically.">',
        '<meta content="Reconciles invoices automatically." name="description">',
    )
    assert missing_seo_requirements(sources) == []


def test_a_missing_sitemap_is_flagged() -> None:
    sources = {k: v for k, v in _GOOD_SOURCES.items() if k != "app/sitemap.xml"}
    missing = missing_seo_requirements(sources)
    assert any("sitemap" in item for item in missing)


def test_a_missing_robots_txt_is_flagged() -> None:
    sources = {k: v for k, v in _GOOD_SOURCES.items() if k != "app/robots.txt"}
    missing = missing_seo_requirements(sources)
    assert any("robots" in item for item in missing)


def test_multiple_gaps_are_all_reported_at_once() -> None:
    """A single heal cycle should be told everything wrong, not just the first
    thing found -- fixing one gap at a time across separate cycles would waste
    heal-cycle budget on problems that could have been fixed together."""
    sources = {"app/index.html": "<html><body>no head at all</body></html>"}
    missing = missing_seo_requirements(sources)
    assert len(missing) == 4  # title, description, sitemap, robots


def test_a_workspace_with_no_html_files_is_skipped_not_flagged() -> None:
    """Both prompts already require a pytest suite asserting the page loads, so a
    passing test run already implies an HTML file exists by the time this check
    runs -- this is a defensive fallback, not the primary enforcement, and must
    not double-penalise a build that has some other, unrelated problem."""
    sources = {"app/server.py": "print('no html here')"}
    assert missing_seo_requirements(sources) == []


def test_the_check_looks_across_every_html_file_not_just_one() -> None:
    """A multi-page product might split title/description across pages during a
    repair -- the check should pass if the requirement is satisfied ANYWHERE in
    the HTML file set, not require every single page to carry every tag."""
    sources = {
        "app/index.html": "<html><head></head><body>no seo tags here</body></html>",
        "app/about.html": _GOOD_HTML,
        "app/sitemap.xml": "<urlset></urlset>",
        "app/robots.txt": "User-agent: *\nAllow: /\n",
    }
    assert missing_seo_requirements(sources) == []
