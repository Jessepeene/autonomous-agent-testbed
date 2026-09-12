"""WebsiteContactProvider: web-search-driven contact scraping.

Ported from a proven, already-in-production reference (`reference files/
lead_scout.py`) — these tests lock in the specific production lessons that file
already paid for: a hallucinated/injected field must never survive extraction, a
platform/job-board domain must never be scraped or enriched against, and a
company+name already known must not spend a second enrichment call.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest

from factory.engines.marketing.providers import website as website_module
from factory.engines.marketing.providers.base import (
    LeadSearchPlan,
    ProviderAuthError,
    ProviderRequestError,
    QualificationBrief,
)
from factory.engines.marketing.providers.website import (
    ExtractedContact,
    WebsiteContactProvider,
    _BorderlineVerdict,
    _domain_matches_company,
    _is_employer_domain,
    _LeadExtraction,
    _QueryList,
)
from factory.integrations.serper import SerperResult
from factory.llm.providers.base import Provider
from factory.llm.providers.fake import FakeProvider
from factory.settings import Settings
from factory.supervisor.context import FactoryContext

_PLAN = LeadSearchPlan(
    job_titles=["Owner"],
    company_keywords=["bookkeeping"],
    rationale="Owners of small bookkeeping practices buy this directly.",
)
_BRIEF = QualificationBrief(
    positive_criteria=["Must be a freelance bookkeeper"],
    strict_exclusions=["Never extract an in-house employee"],
    borderline_guidance="When unsure, prefer to skip.",
)


def _settings(**overrides: object) -> Settings:
    base = {
        "serper_api_key": "serper-test-key",
        "hunter_api_key": "hunter-test-key",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _provider(
    ctx: FactoryContext,
    *,
    settings: Settings | None = None,
    brief: QualificationBrief | None = _BRIEF,
) -> WebsiteContactProvider:
    ctx = dataclasses.replace(ctx, settings=settings if settings is not None else _settings())
    return WebsiteContactProvider(
        ctx,
        session=object(),  # type: ignore[arg-type]  # never touched once repository is mocked
        product_summary="A tool for freelance bookkeepers.",
        qualification_brief=brief,
    )


def _mock_repository(
    monkeypatch: pytest.MonkeyPatch,
    *,
    has_lead: bool = False,
    blocked_domains: set[str] | None = None,
    proxy_domains: set[str] | None = None,
) -> None:
    async def fake_recently_processed(*args: object, **kwargs: object) -> set[str]:
        return set()

    async def fake_has_lead(*args: object, **kwargs: object) -> bool:
        return has_lead

    async def fake_blocked_domains(*args: object, **kwargs: object) -> set[str]:
        return blocked_domains or set()

    async def fake_proxy_domains(*args: object, **kwargs: object) -> set[str]:
        return proxy_domains or set()

    async def fake_record_failure(*args: object, **kwargs: object) -> int:
        return 1

    async def fake_mark_requires_proxy(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        website_module.repository, "recently_processed_urls", fake_recently_processed
    )
    monkeypatch.setattr(website_module.repository, "has_lead_for_company", fake_has_lead)
    monkeypatch.setattr(website_module.repository, "blocked_scrape_domains", fake_blocked_domains)
    monkeypatch.setattr(website_module.repository, "proxy_required_domains", fake_proxy_domains)
    monkeypatch.setattr(website_module.repository, "record_scrape_failure", fake_record_failure)
    monkeypatch.setattr(website_module.repository, "mark_requires_proxy", fake_mark_requires_proxy)


def _mock_serper(monkeypatch: pytest.MonkeyPatch, results: list[SerperResult]) -> None:
    async def fake_search(
        query: str, *, num: int = 10, settings: Settings | None = None
    ) -> list[SerperResult]:
        return results

    monkeypatch.setattr(website_module.serper, "search", fake_search)


def _mock_fetch(
    monkeypatch: pytest.MonkeyPatch,
    page_text: str | None,
    block_reason: str | None = None,
    used_proxy: bool = False,
) -> None:
    async def fake_fetch(
        url: str, *, settings: Settings, requires_proxy: bool
    ) -> tuple[str | None, str | None, bool]:
        return page_text, block_reason, used_proxy

    monkeypatch.setattr(website_module, "_fetch_page_text", fake_fetch)


def _mock_robots_allowed(monkeypatch: pytest.MonkeyPatch, allowed: bool = True) -> None:
    async def fake_allowed(url: str, *, settings: Settings, requires_proxy: bool) -> bool:
        return allowed

    monkeypatch.setattr(website_module, "_robots_allowed", fake_allowed)


# --- available / basic skip paths -------------------------------------------------------


def test_unavailable_without_both_keys(ctx: FactoryContext) -> None:
    provider = _provider(ctx, settings=_settings(hunter_api_key=None))
    assert provider.available is False

    provider2 = _provider(ctx, settings=_settings(serper_api_key=None))
    assert provider2.available is False


async def test_search_returns_empty_when_unavailable(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_repository(monkeypatch)
    provider = _provider(ctx, settings=_settings(serper_api_key=None))

    result = await provider.search(_PLAN)

    assert result == []


async def test_search_returns_empty_when_query_generation_yields_nothing(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_repository(monkeypatch)
    from factory.llm.providers.base import LLMError

    gemini_fake.register(_QueryList, LLMError(Provider.GEMINI, "schema violation"))
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []


# --- the full happy path ----------------------------------------------------------------


async def test_a_page_found_email_is_stored_with_primary_source_provenance(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    page_text = "Ada Lovelace, Owner of Acme Books. Contact: ada@acme-books.test"
    _mock_fetch(monkeypatch, page_text)
    _mock_robots_allowed(monkeypatch, True)

    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    gemini_fake.register(
        _LeadExtraction,
        _LeadExtraction(
            leads=[
                ExtractedContact(
                    name="Ada Lovelace",
                    company="Acme Books",
                    job_title="Owner",
                    email="ada@acme-books.test",
                    confidence=0.95,
                )
            ]
        ),
    )
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert len(result) == 1
    candidate = result[0]
    assert candidate.first_name == "Ada"
    assert candidate.last_name == "Lovelace"
    assert candidate.work_email == "ada@acme-books.test"
    assert candidate.provenance.url == "https://acme-books.test/team"
    assert str(candidate.provenance.kind) == "primary_source"


# --- the anti-hallucination backstop ----------------------------------------------------


async def test_an_extracted_email_not_actually_on_the_page_is_dropped(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model can hallucinate, or the page can carry a prompt-injection
    attempt -- either way, a field that isn't literally on the page must never
    become a stored candidate."""
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(monkeypatch, "Ada Lovelace, Owner. No email published here.")
    _mock_robots_allowed(monkeypatch, True)

    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    gemini_fake.register(
        _LeadExtraction,
        _LeadExtraction(
            leads=[
                ExtractedContact(
                    name="Ada Lovelace",
                    company="Acme Books",
                    job_title="Owner",
                    email="invented@acme-books.test",  # not on the page
                    confidence=0.95,
                )
            ]
        ),
    )
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []


async def test_a_fabricated_job_title_not_actually_on_the_page_is_dropped(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 1 finding #4, system-wide verification plan: job_title used to be
    the one field this backstop never checked, despite being exactly the
    field the LIA later uses to argue role-to-utility link
    (compliance/assessor.py)."""
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(monkeypatch, "Ada Lovelace of Acme Books. Contact: ada@acme-books.test")
    _mock_robots_allowed(monkeypatch, True)

    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    gemini_fake.register(
        _LeadExtraction,
        _LeadExtraction(
            leads=[
                ExtractedContact(
                    name="Ada Lovelace",
                    company="Acme Books",
                    job_title="Chief Financial Officer",  # not on the page anywhere
                    email="ada@acme-books.test",
                    confidence=0.95,
                )
            ]
        ),
    )
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []


async def test_a_paraphrased_job_title_is_still_accepted(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job_title check is deliberately loose (`claim_loosely_in_source`),
    not an exact substring match like company/name/email -- a title is
    routinely paraphrased relative to the page's own wording, and a real,
    correctly-extracted title must not be dropped just because it isn't a
    byte-for-byte match."""
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(
        monkeypatch, "Ada Lovelace, Head of Finance at Acme Books. Contact: ada@acme-books.test"
    )
    _mock_robots_allowed(monkeypatch, True)

    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    gemini_fake.register(
        _LeadExtraction,
        _LeadExtraction(
            leads=[
                ExtractedContact(
                    name="Ada Lovelace",
                    company="Acme Books",
                    job_title="Finance Lead",  # paraphrased, but "Finance" is on the page
                    email="ada@acme-books.test",
                    confidence=0.95,
                )
            ]
        ),
    )
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert len(result) == 1
    assert result[0].job_title == "Finance Lead"


# --- employer-domain / non-employer-domain filtering --------------------------------


async def test_a_linkedin_result_is_never_fetched(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Ada on LinkedIn", link="https://linkedin.com/in/ada", snippet="...")],
    )
    fetch_calls: list[str] = []

    async def fake_fetch(url: str) -> tuple[str | None, str | None]:
        fetch_calls.append(url)
        return "should never be reached", None

    monkeypatch.setattr(website_module, "_fetch_page_text", fake_fetch)
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []
    assert fetch_calls == []


def test_is_employer_domain_blocks_known_platforms() -> None:
    assert _is_employer_domain("linkedin.com") is False
    assert _is_employer_domain("careers.indeed.com") is False
    assert _is_employer_domain("acme-books.test") is True


# --- robots.txt ---------------------------------------------------------------------


async def test_a_robots_disallowed_page_is_never_fetched(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_robots_allowed(monkeypatch, False)
    fetch_calls: list[str] = []

    async def fake_fetch(url: str) -> tuple[str | None, str | None]:
        fetch_calls.append(url)
        return "should never be reached", None

    monkeypatch.setattr(website_module, "_fetch_page_text", fake_fetch)
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []
    assert fetch_calls == []


# --- the domain no-call list ----------------------------------------------------------


async def test_an_already_blocked_domain_is_never_fetched(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain that crossed the failure threshold on an earlier harvest must
    be skipped before a fetch is even attempted on this one."""
    _mock_repository(monkeypatch, blocked_domains={"acme-books.test"})
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    fetch_calls: list[str] = []

    async def fake_fetch(url: str) -> tuple[str | None, str | None]:
        fetch_calls.append(url)
        return "should never be reached", None

    monkeypatch.setattr(website_module, "_fetch_page_text", fake_fetch)
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []
    assert fetch_calls == []


# --- the datacenter-proxy fallback ----------------------------------------------------


async def test_a_domain_already_requiring_the_proxy_is_fetched_with_requires_proxy_true(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A domain already known (from an earlier harvest) to need the proxy must
    have that threaded through to both the robots check and the fetch --
    `_fetch_page_text`/`_robots_allowed` are what actually skip the doomed
    direct attempt, but only if `search()` tells them to."""
    _mock_repository(monkeypatch, proxy_domains={"acme-books.test"})
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))

    robots_calls: list[bool] = []

    async def fake_robots(url: str, *, settings: Settings, requires_proxy: bool) -> bool:
        robots_calls.append(requires_proxy)
        return True

    fetch_calls: list[bool] = []

    async def fake_fetch(
        url: str, *, settings: Settings, requires_proxy: bool
    ) -> tuple[str | None, str | None, bool]:
        fetch_calls.append(requires_proxy)
        return None, None, False

    monkeypatch.setattr(website_module, "_robots_allowed", fake_robots)
    monkeypatch.setattr(website_module, "_fetch_page_text", fake_fetch)
    provider = _provider(ctx)

    await provider.search(_PLAN)

    assert robots_calls == [True]
    assert fetch_calls == [True]


async def test_a_first_time_proxy_success_marks_the_domain_as_requiring_it(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first fetch that succeeds via the proxy for a domain not yet known
    to need it must be remembered -- otherwise every later fetch from the same
    domain keeps paying for a direct attempt already known to fail."""
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(
        monkeypatch,
        "Ada Lovelace, Owner of Acme Books. ada@acme-books.test",
        used_proxy=True,
    )
    _mock_robots_allowed(monkeypatch, True)
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    gemini_fake.register(
        _LeadExtraction,
        _LeadExtraction(
            leads=[
                ExtractedContact(
                    name="Ada Lovelace",
                    company="Acme Books",
                    job_title="Owner",
                    email="ada@acme-books.test",
                    confidence=0.95,
                )
            ]
        ),
    )
    marked: list[str] = []

    async def fake_mark_requires_proxy(session: object, domain: str) -> None:
        marked.append(domain)

    monkeypatch.setattr(website_module.repository, "mark_requires_proxy", fake_mark_requires_proxy)
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert len(result) == 1
    assert marked == ["acme-books.test"]


async def test_a_429_via_the_direct_path_still_counts_toward_blocking_but_not_via_proxy(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`record_scrape_failure` must be told `via_proxy=True` when the failure
    came from the proxy attempt -- a proxy failure must not silently count
    toward blocking a domain on its *direct* threshold, which a different
    egress IP was never going to fix anyway."""
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(monkeypatch, None, "timeout", used_proxy=True)
    _mock_robots_allowed(monkeypatch, True)
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))

    recorded: list[tuple[str, str, bool]] = []

    async def fake_record_failure(
        *args: object, domain: str, reason: str, via_proxy: bool = False
    ) -> int:
        recorded.append((domain, reason, via_proxy))
        return 1

    monkeypatch.setattr(website_module.repository, "record_scrape_failure", fake_record_failure)
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []
    assert recorded == [("acme-books.test", "timeout", True)]


async def test_a_timeout_records_a_scrape_failure(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(monkeypatch, None, "timeout")
    _mock_robots_allowed(monkeypatch, True)
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))

    recorded: list[tuple[str, str, bool]] = []

    async def fake_recently_processed(*args: object, **kwargs: object) -> set[str]:
        return set()

    async def fake_blocked_domains(*args: object, **kwargs: object) -> set[str]:
        return set()

    async def fake_proxy_domains(*args: object, **kwargs: object) -> set[str]:
        return set()

    async def fake_record_failure(
        *args: object, domain: str, reason: str, via_proxy: bool = False
    ) -> int:
        recorded.append((domain, reason, via_proxy))
        return 1

    monkeypatch.setattr(
        website_module.repository, "recently_processed_urls", fake_recently_processed
    )
    monkeypatch.setattr(website_module.repository, "blocked_scrape_domains", fake_blocked_domains)
    monkeypatch.setattr(website_module.repository, "proxy_required_domains", fake_proxy_domains)
    monkeypatch.setattr(website_module.repository, "record_scrape_failure", fake_record_failure)
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []
    assert recorded == [("acme-books.test", "timeout", False)]


async def test_a_second_failure_within_one_call_blocks_the_rest_of_that_call(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once `record_scrape_failure`'s returned count crosses the threshold,
    a *third* result on the same domain within this same `search()` call must
    be skipped without a fetch -- the first two still get attempted (there's
    no way to know a URL will fail before trying it), but by the time the
    count crosses the threshold, later same-domain results in the same call
    don't need to keep paying for it."""
    _mock_serper(
        monkeypatch,
        [
            SerperResult(title="Team", link="https://acme-books.test/team", snippet="..."),
            SerperResult(title="About", link="https://acme-books.test/about", snippet="..."),
            SerperResult(title="Staff", link="https://acme-books.test/staff", snippet="..."),
        ],
    )
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))

    fetch_calls: list[str] = []

    async def fake_fetch(
        url: str, *, settings: Settings, requires_proxy: bool
    ) -> tuple[str | None, str | None, bool]:
        fetch_calls.append(url)
        return None, "forbidden", False

    async def fake_recently_processed(*args: object, **kwargs: object) -> set[str]:
        return set()

    async def fake_blocked_domains(*args: object, **kwargs: object) -> set[str]:
        return set()

    async def fake_proxy_domains(*args: object, **kwargs: object) -> set[str]:
        return set()

    async def fake_record_failure(
        *args: object, domain: str, reason: str, via_proxy: bool = False
    ) -> int:
        return len(fetch_calls)  # 1st failure -> 1, 2nd -> 2 (meets threshold)

    monkeypatch.setattr(website_module, "_fetch_page_text", fake_fetch)
    monkeypatch.setattr(
        website_module.repository, "recently_processed_urls", fake_recently_processed
    )
    monkeypatch.setattr(website_module.repository, "blocked_scrape_domains", fake_blocked_domains)
    monkeypatch.setattr(website_module.repository, "proxy_required_domains", fake_proxy_domains)
    monkeypatch.setattr(website_module.repository, "record_scrape_failure", fake_record_failure)
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []
    assert fetch_calls == [
        "https://acme-books.test/team",
        "https://acme-books.test/about",
    ]  # the third URL was skipped once the domain crossed the threshold


# --- confidence gating + borderline verification ------------------------------------


async def test_low_confidence_extraction_is_dropped_without_verification(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(monkeypatch, "Ada Lovelace, Owner of Acme Books. ada@acme-books.test")
    _mock_robots_allowed(monkeypatch, True)

    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    gemini_fake.register(
        _LeadExtraction,
        _LeadExtraction(
            leads=[
                ExtractedContact(
                    name="Ada Lovelace",
                    company="Acme Books",
                    job_title="Owner",
                    email="ada@acme-books.test",
                    confidence=0.2,  # below _MIN_CONFIDENCE
                )
            ]
        ),
    )
    # No _BorderlineVerdict registered -- if verification were attempted, FakeProvider
    # would raise, failing this test.
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []


async def test_borderline_confidence_is_dropped_when_verification_says_no(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(monkeypatch, "Ada Lovelace, Owner of Acme Books. ada@acme-books.test")
    _mock_robots_allowed(monkeypatch, True)

    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    gemini_fake.register(
        _LeadExtraction,
        _LeadExtraction(
            leads=[
                ExtractedContact(
                    name="Ada Lovelace",
                    company="Acme Books",
                    job_title="Owner",
                    email="ada@acme-books.test",
                    confidence=0.6,  # between _MIN_CONFIDENCE and _VERIFY_CONFIDENCE_THRESHOLD
                )
            ]
        ),
    )
    gemini_fake.register(_BorderlineVerdict, _BorderlineVerdict(qualifies=False))
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []


async def test_borderline_confidence_is_kept_when_verification_says_yes(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_repository(monkeypatch)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(monkeypatch, "Ada Lovelace, Owner of Acme Books. ada@acme-books.test")
    _mock_robots_allowed(monkeypatch, True)

    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    gemini_fake.register(
        _LeadExtraction,
        _LeadExtraction(
            leads=[
                ExtractedContact(
                    name="Ada Lovelace",
                    company="Acme Books",
                    job_title="Owner",
                    email="ada@acme-books.test",
                    confidence=0.6,
                )
            ]
        ),
    )
    gemini_fake.register(_BorderlineVerdict, _BorderlineVerdict(qualifies=True))
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert len(result) == 1


# --- pre-enrichment dedup ------------------------------------------------------------


async def test_a_company_and_name_already_known_is_skipped(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_repository(monkeypatch, has_lead=True)
    _mock_serper(
        monkeypatch,
        [SerperResult(title="Our Team", link="https://acme-books.test/team", snippet="...")],
    )
    _mock_fetch(monkeypatch, "Ada Lovelace, Owner of Acme Books. ada@acme-books.test")
    _mock_robots_allowed(monkeypatch, True)

    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))
    gemini_fake.register(
        _LeadExtraction,
        _LeadExtraction(
            leads=[
                ExtractedContact(
                    name="Ada Lovelace",
                    company="Acme Books",
                    job_title="Owner",
                    email="ada@acme-books.test",
                    confidence=0.95,
                )
            ]
        ),
    )
    provider = _provider(ctx)

    result = await provider.search(_PLAN)

    assert result == []


# --- Serper auth/request errors propagate as ProviderAuthError/ProviderRequestError -----


@pytest.mark.parametrize("status", [401, 403])
async def test_serper_auth_rejection_raises_provider_auth_error(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _mock_repository(monkeypatch)
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="Invalid API Key", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = _provider(ctx)

    with pytest.raises(ProviderAuthError):
        await provider.search(_PLAN)


@pytest.mark.parametrize("status", [400, 422])
async def test_serper_request_rejection_raises_provider_request_error(
    ctx: FactoryContext, gemini_fake: FakeProvider, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    _mock_repository(monkeypatch)
    gemini_fake.register(_QueryList, _QueryList(queries=["bookkeeping contact us"]))

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(status, text="Malformed request", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    provider = _provider(ctx)

    with pytest.raises(ProviderRequestError):
        await provider.search(_PLAN)


# --- domain-matching guard, pure function -----------------------------------------------


def test_domain_matches_company_requires_a_distinctive_word() -> None:
    assert _domain_matches_company("Acme Books LLC", "acme-books.test") is True
    # A generic directory domain shares no distinctive word with the company name.
    assert _domain_matches_company("Acme Books LLC", "ibew48.test") is False


def test_domain_matches_company_fails_closed_with_no_distinctive_words() -> None:
    assert _domain_matches_company("The Company LLC", "example.test") is False
