"""What the cheap deterministic gates do when they find something.

These used to short-circuit: a missing <title> healed immediately, before Bug
Council, the requirement comparison or anything else ran. The reasoning was sound
when Council was three Opus reviewers -- paying for them to review code that was
going to heal anyway is waste.

It has since inverted. Under the current routing a full Council pass costs cents,
while the thing serial discovery actually costs is a *rebuild per finding*: fix the
title, rebuild, meet the contract complaint, rebuild, meet the Council findings,
rebuild. Finding everything in one pass is now both cheaper and more useful, which
is what a human running CI gets by default.

What survives is the distinction between the two gates, which was always the real
one. SEO is quality -- a page with no <title> is worse, not broken -- so it is
recorded and weighed. The codegen contract is environment facts nothing else in the
company knows: which exact key name the deploy pipeline sets, which exact URL
captured emails must reach. Getting one wrong is silent, total breakage, so it
still holds a release.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from factory.engines.development import bug_council
from factory.engines.development import graph as dev_graph
from factory.sandbox.runner import SandboxResult
from factory.state import repository
from factory.state.schemas import CouncilResult, ProductStatus
from factory.supervisor.context import FactoryContext

#: Set per-test where the build kind matters; `_verify_and_ship` reads it off the
#: product row the stub returns.
_build_kind = {"value": "mvp"}

#: What the gate checks filed this test, for assertions below.
recorded_issues: list[dict[str, object]] = []


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_verify_and_ship` writes to the database at several points (status
    transitions, resolving heal attempts, filing what the gates found) -- all
    stubbed rather than standing up a real session, matching
    test_devops_error_routing.py's identical fixture for the first of these."""
    _build_kind["value"] = "mvp"
    recorded_issues.clear()

    async def fake_set_product_status(
        session: object, product_id: str, status: ProductStatus, **fields: object
    ) -> None:
        return None

    async def fake_resolve_pending_heal_attempts(
        session: object, product_id: str, outcome: str
    ) -> None:
        return None

    async def fake_enqueue_task(session: object, **kwargs: object) -> str | None:
        return "task-1"

    async def fake_get_product(session: object, product_id: str) -> None:
        # None short-circuits _verify_and_ship's domain-request check (a
        # freshly-live product it cannot look up gets asked about a domain on
        # some later attempt instead of this one) -- exactly like every other
        # stub here, real domain-request behaviour has its own dedicated tests.
        return None

    async def fake_get_build_kind(session: object, product_id: str) -> str:
        # The one fact the gate reads, stubbed on its own. It used to be a
        # parameter, and both repair call sites dropped it -- so a landing page
        # was re-verified as an MVP on every fix and the contract demanded Stripe
        # wiring its own requirements forbade.
        return _build_kind["value"]

    @dataclass(slots=True)
    class _FakeIdeaRow:
        self_serve_price_usd: float = 49.0

    async def fake_get_idea(session: object, idea_id: str) -> _FakeIdeaRow:
        # Only reached for a real (non-validation) build, right before deploy --
        # see _verify_and_ship's price lookup.
        return _FakeIdeaRow()


    async def fake_live_requirements(
        session: object, subject_kind: str, subject_id: str
    ) -> None:
        # No stated standard, which is the pre-requirements world every one of
        # these tests was written against -- `decide_release` says so in its
        # rationale rather than inventing one.
        return None

    async def fake_open_issues_for(session: object, product_id: str) -> list[object]:
        return []

    async def fake_record_task_event(session: object, **kwargs: object) -> None:
        return None


    async def fake_record_issue(session: object, **kwargs: object) -> tuple[object, bool]:
        # The SEO and contract checks now file what they find instead of healing on
        # it, so this fixture needs the write path stubbed like every other one here.
        recorded_issues.append(kwargs)
        return object(), True


    async def fake_product_progress(session: object, product_id: str) -> object:
        # The convergence thread, assembled from rows in production. Nothing here
        # exercises it, so a first-attempt shape (nothing to compare against) keeps
        # the release judgement on its normal path.
        from factory.state.repository import ProductProgress

        return ProductProgress(
            attempts=0,
            blocking_now=0,
            blocking_at_last_attempt=None,
            stuck=[],
            last_decision=None,
        )

    monkeypatch.setattr(repository, "set_product_status", fake_set_product_status)
    monkeypatch.setattr(repository, "product_progress", fake_product_progress)
    monkeypatch.setattr(repository, "record_issue", fake_record_issue)
    monkeypatch.setattr(repository, "live_requirements", fake_live_requirements)
    monkeypatch.setattr(repository, "open_issues_for", fake_open_issues_for)
    monkeypatch.setattr(repository, "record_task_event", fake_record_task_event)
    monkeypatch.setattr(
        repository, "resolve_pending_heal_attempts", fake_resolve_pending_heal_attempts
    )
    monkeypatch.setattr(repository, "enqueue_task", fake_enqueue_task)
    monkeypatch.setattr(repository, "get_product", fake_get_product)
    monkeypatch.setattr(repository, "get_build_kind", fake_get_build_kind)
    monkeypatch.setattr(repository, "get_idea", fake_get_idea)


@dataclass(slots=True)
class _ScriptedSandbox:
    results: list[SandboxResult]
    calls: list[dict[str, object]] = field(default_factory=list)
    available: bool = True

    async def execute(
        self,
        command: str,
        *,
        workspace: str,
        timeout_seconds: int | None = None,
        network: bool = False,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        self.calls.append({"command": command, "network": network, "env": env})
        return self.results.pop(0)


def _passing_sandbox() -> _ScriptedSandbox:
    return _ScriptedSandbox(
        results=[
            SandboxResult(exit_code=0, stdout="", stderr=""),  # install
            SandboxResult(exit_code=0, stdout="5 passed", stderr=""),  # tests
        ]
    )


def _write_workspace(ctx: FactoryContext, workspace: str, files: dict[str, str]) -> None:
    root = ctx.settings.workspace_root / workspace
    for path, contents in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")


_INCOMPLETE_HTML = "<html><head></head><body><main>no seo tags here</main></body></html>"

_COMPLETE_HTML = (
    "<html><head><title>Real product</title>"
    '<meta name="description" content="Does a real thing."></head>'
    '<body><main>hi</main><script>fetch("https://example.test/analytics/prod-1")</script>'
    '<a href="privacy.html">Privacy</a></body></html>'
)

_COMPLETE_BACKEND_PY = (
    "import os\n"
    "stripe_api_key = os.environ['STRIPE_API_KEY']\n"
    "price_cents = int(os.environ['PRODUCT_PRICE_CENTS'])\n"
    "client_reference_id = os.environ['PRODUCT_ID']\n"
)


async def test_a_missing_seo_requirement_is_recorded_and_does_not_short_circuit(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.sandbox = _passing_sandbox()
    _write_workspace(ctx, "product-1", {"app/index.html": _INCOMPLETE_HTML})

    convened = False

    async def fake_convene(*args: object, **kwargs: object) -> CouncilResult:
        nonlocal convened
        convened = True
        return CouncilResult(approved=True, verdicts=[])

    healed_with: dict[str, object] = {}

    async def fake_heal(
        ctx: FactoryContext,
        session: object,
        product_id: str,
        healing: str,
        idea_id: str,
        *,
        category: str,
    ) -> str:
        healed_with["category"] = category
        healed_with["healing"] = healing
        return "healed"

    monkeypatch.setattr(bug_council, "convene", fake_convene)
    monkeypatch.setattr(dev_graph, "_heal", fake_heal)

    # Deliberately unused: SEO no longer decides the outcome on its own, so what
    # this returns depends on the release judgement rather than on the gate. What
    # matters is that the rest of verification ran.
    _ = await dev_graph._verify_and_ship(
        ctx,
        None,  # type: ignore[arg-type]
        product_id="prod-1",
        product_name="Test Product",
        workspace="product-1",
        test_command="pytest -q",
        idea_id="idea-1",
    )

    # The short circuit is gone, and the reasoning behind it inverted rather than
    # being ignored. It existed because Council was three Opus reviewers, so paying
    # for them to review code that was going to heal anyway was waste. Under the
    # current routing a full Council pass is cents, while a rebuild cycle -- which is
    # what serial discovery costs, once per finding -- is the expensive part. Finding
    # everything at once is now the cheaper order as well as the more useful one.
    assert convened is True, "everything must be looked at in one pass, not one finding at a time"
    # SEO is quality, not breakage: a page with no <title> is worse, not broken, so
    # it is recorded and weighed rather than holding the release by itself.
    assert healed_with.get("category") != "seo_incomplete"


async def test_a_complete_page_reaches_bug_council_normally(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx.sandbox = _passing_sandbox()
    _write_workspace(
        ctx,
        "product-1",
        {
            "app/index.html": _COMPLETE_HTML,
            "app/sitemap.xml": "<urlset></urlset>",
            "app/robots.txt": "User-agent: *\nAllow: /\n",
            "app/main.py": _COMPLETE_BACKEND_PY,
            "app/privacy.html": "<html><body>We collect an email address.</body></html>",
        },
    )

    async def fake_convene(*args: object, **kwargs: object) -> CouncilResult:
        return CouncilResult(approved=True, verdicts=[])

    async def fake_deploy(*args: object, **kwargs: object) -> object:
        from factory.engines.development import deploy as deploy_module

        return deploy_module.DeployResult(ok=True, url="https://example.test", detail="ok")

    heal_called = False

    async def fake_heal(*args: object, **kwargs: object) -> str:
        nonlocal heal_called
        heal_called = True
        return "should not be reached"

    monkeypatch.setattr(bug_council, "convene", fake_convene)
    monkeypatch.setattr(dev_graph.deploy_module, "deploy", fake_deploy)
    monkeypatch.setattr(dev_graph, "_heal", fake_heal)

    result = await dev_graph._verify_and_ship(
        ctx,
        None,  # type: ignore[arg-type]
        product_id="prod-1",
        product_name="Test Product",
        workspace="product-1",
        test_command="pytest -q",
        idea_id="idea-1",
    )

    assert heal_called is False
    assert "deployed" in result


async def test_seo_incomplete_is_a_surgical_category() -> None:
    assert "seo_incomplete" in dev_graph.SURGICAL_CATEGORIES


async def test_a_missing_contract_requirement_still_holds_the_release(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace that satisfies SEO but not the codegen contract (a backend file
    exists but never reads PRODUCT_PRICE_CENTS) must heal on `contract_incomplete`,
    not reach Bug Council -- same ordering guarantee as the SEO gate above. The
    backend file must actually exist here: `missing_contract_requirements` defers
    to a defensive skip when there are zero .py files at all (mirroring seo.py's
    own "no HTML files" skip), so a workspace with literally no backend wouldn't
    exercise this specific flagging path."""
    ctx.sandbox = _passing_sandbox()
    _write_workspace(
        ctx,
        "product-1",
        {
            "app/index.html": _COMPLETE_HTML,
            "app/sitemap.xml": "<urlset></urlset>",
            "app/robots.txt": "User-agent: *\nAllow: /\n",
            "app/privacy.html": "<html><body>We collect an email address.</body></html>",
            "app/main.py": "print('a backend that never reads PRODUCT_PRICE_CENTS')\n",
        },
    )

    convened = False

    async def fake_convene(*args: object, **kwargs: object) -> CouncilResult:
        nonlocal convened
        convened = True
        return CouncilResult(approved=True, verdicts=[])

    healed_with: dict[str, object] = {}

    async def fake_heal(
        ctx: FactoryContext,
        session: object,
        product_id: str,
        healing: str,
        idea_id: str,
        *,
        category: str,
    ) -> str:
        healed_with["category"] = category
        healed_with["healing"] = healing
        return "healed"

    monkeypatch.setattr(bug_council, "convene", fake_convene)
    monkeypatch.setattr(dev_graph, "_heal", fake_heal)

    result = await dev_graph._verify_and_ship(
        ctx,
        None,  # type: ignore[arg-type]
        product_id="prod-1",
        product_name="Test Product",
        workspace="product-1",
        test_command="pytest -q",
        idea_id="idea-1",
    )

    # The rail that does not move: an environment fact nothing else in the company
    # knows -- which exact key name the deploy pipeline sets -- is silent, total
    # breakage if wrong. Billing fails closed. It still holds the release.
    assert result == "healed"
    assert "PRODUCT_PRICE_CENTS" in str(healed_with["healing"])
    # But it no longer pre-empts everything else. It used to short-circuit, so a
    # build meeting this one gate's complaint was rebuilt only to meet the next --
    # serial discovery, one rebuild per finding. The brief now carries everything
    # found in a single pass.
    assert convened is True, "the rest of verification must still run"


async def test_a_validation_page_is_not_flagged_for_missing_stripe_wiring(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landing page has no Stripe/PRODUCT_ID wiring by design (LANDING_PAGE_SYSTEM's
    own "This is deliberately NOT a real product" section) -- the contract gate must
    not check for PRODUCT_PRICE_CENTS/client_reference_id when is_validation=True, or
    every single validation build would heal forever on a requirement that doesn't
    apply to it.

    The kind is now read from the product rather than passed in, because as a
    parameter both repair call sites dropped it -- so this exemption held on the
    first build and silently stopped holding on every repair after it."""
    _build_kind["value"] = "validation"
    ctx.sandbox = _passing_sandbox()
    _write_workspace(
        ctx,
        "product-1",
        {
            "app/index.html": (
                "<html><head><title>Real product</title>"
                '<meta name="description" content="Does a real thing."></head>'
                '<body><main>hi</main><form action="/interest/prod-1"></form>'
                '<script>fetch("https://example.test/interest/prod-1");'
                'fetch("https://example.test/analytics/prod-1")</script>'
                '<a href="privacy.html">Privacy</a></body></html>'
            ),
            "app/sitemap.xml": "<urlset></urlset>",
            "app/robots.txt": "User-agent: *\nAllow: /\n",
            "app/privacy.html": "<html><body>We only use your email to notify you.</body></html>",
        },
    )

    async def fake_convene(*args: object, **kwargs: object) -> CouncilResult:
        return CouncilResult(approved=True, verdicts=[])

    async def fake_deploy(*args: object, **kwargs: object) -> object:
        from factory.engines.development import deploy as deploy_module

        return deploy_module.DeployResult(ok=True, url="https://example.test", detail="ok")

    heal_called = False

    async def fake_heal(*args: object, **kwargs: object) -> str:
        nonlocal heal_called
        heal_called = True
        return "should not be reached"

    monkeypatch.setattr(bug_council, "convene", fake_convene)
    monkeypatch.setattr(dev_graph.deploy_module, "deploy", fake_deploy)
    monkeypatch.setattr(dev_graph, "_heal", fake_heal)

    result = await dev_graph._verify_and_ship(
        ctx,
        None,  # type: ignore[arg-type]
        product_id="prod-1",
        product_name="Test Product",
        workspace="product-1",
        test_command="pytest -q",
        idea_id="idea-1",
    )

    assert heal_called is False
    assert "deployed" in result


async def test_contract_incomplete_is_a_surgical_category() -> None:
    assert "contract_incomplete" in dev_graph.SURGICAL_CATEGORIES
