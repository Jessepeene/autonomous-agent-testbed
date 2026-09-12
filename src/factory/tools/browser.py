"""Browser tools, built fresh for one product review.

The gap these close: every existing way this factory looks at a shipped product
reads its *markup*. `maintenance.check_deploy_url` fetches the HTML,
`_content_freshness_issue` hands that HTML to a reviewer, `http_get` returns
`response.text`. None of them ever *uses* the page. A wrong year is visible in
markup; a signup form that silently fails on submit is not -- and that is exactly
the pair of defects that sat unnoticed on a live product for three weeks while
both checks reported it healthy.

Three deliberate properties, each load-bearing:

**Private registry, not the process-wide ``REGISTRY``.** Same reasoning as
``tools/workspace.py::build_workspace_tools``: ``REGISTRY.register()`` raises on a
duplicate name, so tools that close over per-run state cannot live in it. The
browser page *is* per-run state -- navigate, then click, then read only means
anything against one page -- and tool handlers receive nothing but the model's own
arguments (no ctx, no session, no run id), so closing over the page is the only
way to hold it across steps without a module-level singleton that two concurrent
reviews would fight over.

**Navigation is pinned to an origin allowlist.** ``http_get`` has no SSRF guard at
all -- its only check is the scheme prefix, so ``http://169.254.169.254/`` and
``http://postgres:5432/`` are both reachable today. A real browser is strictly
worse in that respect: it follows subresource requests and JS-initiated
navigations, and it runs inside the compose network. The reviewer only ever needs
the product's own origin, so that is all it gets.

**Playwright is imported lazily, inside ``__aenter__``.** The package is an
optional extra (`pyproject.toml`'s `[browser]`) installed only in the Docker
image, so importing it at module scope would break every local test run and any
environment that never reviews a product.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any
from urllib.parse import urlparse

from factory.logging import get_logger
from factory.tools.registry import ToolDescriptor, ToolRegistry

log = get_logger(__name__)

#: Visible-text budget per read. The runtime truncates any tool result at 20k, but
#: a review typically reads the same page several times across its steps, so a
#: tighter per-read bound is what actually keeps the transcript affordable.
MAX_PAGE_TEXT_CHARS = 8_000

#: Enumerated form controls per read. A real pitch page has a handful; anything
#: past this is a generated list the reviewer does not need in full.
MAX_FORM_FIELDS = 40

#: Per-operation ceiling. Comfortably inside `runtime._TOOL_TIMEOUT_SECONDS` (120),
#: which would otherwise cancel the call and leave the page in an unknown state.
_NAV_TIMEOUT_MS = 20_000
_ACTION_TIMEOUT_MS = 10_000

#: Network/console lines reported per action. A page load fires dozens of asset
#: requests; the ones that matter for "did my click do anything" are the last few.
_MAX_INSTRUMENT_ROWS = 12

#: How long to let the network settle after a click before snapshotting. Short --
#: this is "did the submit fire", not "wait for the whole page to finish".
_SETTLE_TIMEOUT_MS = 3_000

#: Collected in one `page.evaluate` rather than several Playwright round trips --
#: what the reviewer needs is "what can I interact with here", which is one
#: question, and one evaluate keeps it inside a single step's latency budget.
_FORM_FIELDS_JS = """
() => Array.from(document.querySelectorAll('input, textarea, select, button'))
    .filter(el => {
        const style = window.getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden';
    })
    .map(el => ({
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        name: el.getAttribute('name') || '',
        id: el.getAttribute('id') || '',
        placeholder: el.getAttribute('placeholder') || '',
        text: (el.innerText || el.value || '').slice(0, 80),
        required: el.hasAttribute('required'),
    }))
"""


def origin_of(url: str) -> str | None:
    """``scheme://host[:port]`` lowercased, or ``None`` for anything that is not an
    absolute http(s) URL. ``None`` is the refusal signal every caller checks."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}".lower()


def allowed_origins_for(*urls: str | None) -> set[str]:
    """The origin allowlist for a review: the product's own deploy URL, plus its
    custom domain once it has one. Unparseable or missing entries are dropped
    rather than raising -- a product with no custom domain is the normal case."""
    origins = set()
    for url in urls:
        if not url:
            continue
        origin = origin_of(url)
        if origin is not None:
            origins.add(origin)
    return origins


class BrowserSession:
    """An async context manager yielding a private ``ToolRegistry`` bound to one
    live browser page.

    Usage mirrors `development/repair.py`'s injected workspace registry::

        async with BrowserSession(allowed_origins=origins) as tools:
            runtime = AgentRuntime(ctx.llm, settings=ctx.settings, tools=tools)
            await runtime.run(...)

    The browser is launched on entry and always closed on exit, including on an
    exception -- a leaked Chromium would hold ~200MB resident on a 3.8GB host with
    no swap.
    """

    def __init__(self, *, allowed_origins: set[str]) -> None:
        self._allowed = allowed_origins
        self._playwright: Any = None
        self._browser: Any = None
        self._page: Any = None
        #: Instruments. Drained into each snapshot and cleared, so what an action
        #: reports is what *that action* caused -- see `_drain_instruments`.
        self._requests: list[dict[str, Any]] = []
        self._console: list[str] = []

    async def __aenter__(self) -> ToolRegistry:
        # Deferred: `playwright` is an optional extra, present only in the Docker
        # image. A module-scope import would break every environment that never
        # reviews a product, including the local test run.
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        self._page = await self._browser.new_page()
        self._page.set_default_timeout(_ACTION_TIMEOUT_MS)

        # The instruments an expert tester actually uses. Without these the agent
        # can only compare before/after page text, which is why the first live
        # runs flailed: asked whether a form works, it had no way to see that
        # submitting fired POST /api/subscribe -> 404, so it re-filled and
        # re-clicked hoping the *visible* page would change. A network line
        # settles in one action what visual diffing never settles at all.
        def _on_response(response: Any) -> None:
            try:
                if response.request.resource_type in ("xhr", "fetch", "document"):
                    self._requests.append(
                        {
                            "method": response.request.method,
                            "url": response.url[:200],
                            "status": response.status,
                        }
                    )
            except Exception:  # noqa: S110 - an instrument must never break the run
                pass

        def _on_failed(request: Any) -> None:
            try:
                self._requests.append(
                    {
                        "method": request.method,
                        "url": request.url[:200],
                        "status": f"FAILED ({request.failure})",
                    }
                )
            except Exception:  # noqa: S110 - as above
                pass

        def _on_console(message: Any) -> None:
            try:
                if message.type in ("error", "warning"):
                    self._console.append(f"{message.type}: {message.text[:200]}")
            except Exception:  # noqa: S110 - as above
                pass

        self._page.on("response", _on_response)
        self._page.on("requestfailed", _on_failed)
        self._page.on("console", _on_console)
        return self._build_registry()

    def _drain_instruments(self) -> dict[str, Any]:
        """Network and console activity since the last drain, then reset.

        Per-action rather than cumulative on purpose: "clicking Join fired this
        request" is the causal claim the reviewer needs, and a cumulative log
        makes that exact question harder to answer, not easier.
        """
        requests, console = self._requests[-_MAX_INSTRUMENT_ROWS:], self._console[
            -_MAX_INSTRUMENT_ROWS:
        ]
        self._requests, self._console = [], []
        return {"network": requests, "console_errors": console}

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for closer in (self._browser, self._playwright):
            if closer is None:
                continue
            try:
                await (closer.close() if closer is self._browser else closer.stop())
            except Exception as close_exc:  # never mask the real error
                log.warning("browser.close_failed", detail=str(close_exc)[:200])
        self._page = self._browser = self._playwright = None

    # -- the tools ---------------------------------------------------------

    def _refuse_origin(self, url: str) -> dict[str, Any] | None:
        origin = origin_of(url)
        if origin is None:
            return {"error": f"{url!r} is not an absolute http(s) URL"}
        if origin not in self._allowed:
            return {
                "error": (
                    f"{origin} is not this product's own origin. You may only browse "
                    f"{', '.join(sorted(self._allowed)) or 'nothing -- no origin is configured'}."
                )
            }
        return None

    async def _snapshot(self) -> dict[str, Any]:
        """Everything the reviewer needs to judge the current page in one payload."""
        page = self._page
        assert page is not None
        try:
            text = await page.inner_text("body")
        except Exception as exc:  # a page with no body yet, or mid-navigation
            text = ""
            log.debug("browser.inner_text_failed", detail=str(exc)[:200])
        try:
            fields = await page.evaluate(_FORM_FIELDS_JS)
        except Exception as exc:
            fields = []
            log.debug("browser.fields_failed", detail=str(exc)[:200])
        return {
            "url": page.url,
            "title": await page.title(),
            "text": text[:MAX_PAGE_TEXT_CHARS],
            "text_truncated": len(text) > MAX_PAGE_TEXT_CHARS,
            "form_fields": fields[:MAX_FORM_FIELDS],
            **self._drain_instruments(),
        }

    def _build_registry(self) -> ToolRegistry:
        registry = ToolRegistry()

        async def browser_open(url: str) -> dict[str, Any]:
            refusal = self._refuse_origin(url)
            if refusal is not None:
                return refusal
            page = self._page
            assert page is not None
            try:
                response = await page.goto(url, timeout=_NAV_TIMEOUT_MS, wait_until="load")
            except Exception as exc:
                return {"error": f"could not load {url}: {str(exc)[:300]}"}
            snapshot = await self._snapshot()
            snapshot["status_code"] = response.status if response is not None else None
            return snapshot

        async def browser_read() -> dict[str, Any]:
            if self._page is None or not self._page.url or self._page.url == "about:blank":
                return {"error": "no page is open yet -- call browser_open first"}
            return await self._snapshot()

        async def browser_click(target: str) -> dict[str, Any]:
            page = self._page
            if page is None or page.url == "about:blank":
                return {"error": "no page is open yet -- call browser_open first"}
            before_url = page.url
            # Visible text first: it is what the reviewer actually sees and what a
            # real visitor would click. A CSS selector is the fallback for controls
            # with no text (an icon button, a bare input). Both failures are reported
            # together -- "no text matched, and it is not a valid selector either" is
            # what tells the agent to go re-read the page rather than retry blindly.
            failures: list[str] = []
            for how, attempt in (
                ("visible text", page.get_by_text(target, exact=False).first),
                ("css selector", page.locator(target)),
            ):
                try:
                    await attempt.click(timeout=_ACTION_TIMEOUT_MS)
                    break
                except Exception as exc:
                    failures.append(f"{how}: {str(exc).splitlines()[0][:120]}")
            else:
                log.debug("browser.click_unmatched", target=target[:80])
                return {
                    "error": f"nothing clickable matched {target!r}",
                    "attempts": failures,
                }
            # Let any request the click triggered actually land before snapshotting,
            # or the network instrument reports nothing and the agent concludes
            # "clicking did nothing" from a race rather than from the truth.
            try:
                await page.wait_for_load_state("networkidle", timeout=_SETTLE_TIMEOUT_MS)
            except Exception:  # noqa: S110 - a page that never idles is not an error
                pass
            snapshot = await self._snapshot()
            snapshot["navigated"] = snapshot["url"] != before_url
            return snapshot

        async def browser_fill(selector: str, value: str) -> dict[str, Any]:
            page = self._page
            if page is None or page.url == "about:blank":
                return {"error": "no page is open yet -- call browser_open first"}
            try:
                await page.fill(selector, value, timeout=_ACTION_TIMEOUT_MS)
            except Exception as exc:
                return {"error": f"could not fill {selector!r}: {str(exc)[:300]}"}
            return {"filled": selector, "value": value}

        registry.register(
            ToolDescriptor(
                name="browser_open",
                description=(
                    "Open a URL in a real browser and return the rendered page: its "
                    "title, visible text, and every form field on it. This runs "
                    "JavaScript, so it shows what a visitor actually sees -- not the "
                    "raw HTML. Start here."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Absolute http(s) URL."}
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
                category="browser",
                handler=browser_open,
            )
        )
        registry.register(
            ToolDescriptor(
                name="browser_read",
                description=(
                    "Re-read the page you are currently on. Use this after clicking or "
                    "filling something to see what actually changed."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                category="browser",
                handler=browser_read,
            )
        )
        registry.register(
            ToolDescriptor(
                name="browser_click",
                description=(
                    "Click something on the current page, by its visible text (e.g. "
                    "'Get early access') or by a CSS selector. Returns the page "
                    "afterwards PLUS `network` (every request the click fired, with "
                    "its status code) and `console_errors`. Read `network` to decide "
                    "whether a submit actually worked -- a form that posts and gets "
                    "404/500, or that fires no request at all, is broken even when "
                    "the page looks unchanged."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "target": {
                            "type": "string",
                            "description": "Visible text of the control, or a CSS selector.",
                            "minLength": 1,
                            "maxLength": 200,
                        }
                    },
                    "required": ["target"],
                    "additionalProperties": False,
                },
                category="browser",
                handler=browser_click,
            )
        )
        registry.register(
            ToolDescriptor(
                name="browser_fill",
                description=(
                    "Type a value into a form field, addressed by CSS selector (e.g. "
                    "\"input[type='email']\"). Use the form_fields list from "
                    "browser_open/browser_read to find the right one."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for the field.",
                            "minLength": 1,
                            "maxLength": 200,
                        },
                        "value": {
                            "type": "string",
                            "description": "Text to type in.",
                            "maxLength": 500,
                        },
                    },
                    "required": ["selector", "value"],
                    "additionalProperties": False,
                },
                category="browser",
                handler=browser_fill,
            )
        )
        return registry
