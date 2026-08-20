"""The deploy gate: deterministic scanners, unanimity, and healing instructions."""

from __future__ import annotations

import pytest

from factory.engines.development import bug_council
from factory.engines.development.deploy import DeployNotPermitted, deploy_requires_approval
from factory.engines.development.scaffold.generator import (
    UnsafeGeneratedPath,
    read_workspace,
    safe_target,
)
from factory.llm.providers.base import Provider, ProviderUnavailable
from factory.llm.providers.fake import FakeProvider
from factory.state.schemas import (
    CouncilResult,
    ReviewCategory,
    ReviewVerdict,
    ScaffoldPlan,
)
from factory.supervisor.context import FactoryContext

CLEAN_SOURCES = {
    "app.py": (
        "import os\nAPI_KEY = os.environ['SERVICE_API_KEY']\ndef add(a, b):\n    return a + b\n"
    )
}


# --- deterministic secret scan ---------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("config.py", 'AWS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'),
        ("key.pem", "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n"),
        ("s.py", 'STRIPE = "sk_live_abcdefghijklmnop1234"\n'),
        ("g.py", 'GOOGLE = "AIzaSyA12345678901234567890123456789012"\n'),
        ("h.py", 'TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"\n'),
    ],
)
def test_secret_scan_catches_hardcoded_credentials(filename: str, content: str) -> None:
    verdict = bug_council.scan_secrets({filename: content})
    assert verdict.approved is False
    assert verdict.findings


def test_secret_scan_catches_high_entropy_assignment() -> None:
    sources = {"conf.py": 'password = "Xk9$mQ2vLp7wRt4zBn8H"\n'}
    verdict = bug_council.scan_secrets(sources)
    assert verdict.approved is False


def test_secret_scan_allows_environment_lookups() -> None:
    verdict = bug_council.scan_secrets(CLEAN_SOURCES)
    assert verdict.approved is True
    assert verdict.findings == []


# --- deterministic privilege scan ------------------------------------------


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("run.sh", "docker run --privileged myimage\n"),
        ("compose.yml", "    - /var/run/docker.sock:/var/run/docker.sock\n"),
        ("run.sh", "docker run --cap-add SYS_ADMIN img\n"),
        ("setup.sh", "sudo apt-get install -y curl\n"),
        ("Dockerfile", "USER root\n"),
        ("x.py", "subprocess.run(cmd, shell=True)\n"),
        ("y.py", "eval(user_input)\n"),
        ("z.sh", "chmod -R 777 /app\n"),
    ],
)
def test_privilege_scan_catches_escalation(filename: str, content: str) -> None:
    verdict = bug_council.scan_privilege({filename: content})
    assert verdict.approved is False
    assert verdict.findings


def test_privilege_scan_allows_ordinary_code() -> None:
    assert bug_council.scan_privilege(CLEAN_SOURCES).approved is True


# --- unanimity --------------------------------------------------------------


def _approving(category: ReviewCategory) -> ReviewVerdict:
    return ReviewVerdict(category=category, approved=True, findings=[])


async def test_council_requires_unanimity(ctx: FactoryContext) -> None:
    """Three model reviewers approve, but a planted secret must still block."""
    anthropic_fake = _anthropic_of(ctx)
    for category in bug_council.REVIEWER_PROMPTS:
        anthropic_fake.register(ReviewVerdict, _approving(category))

    result = await bug_council.convene(ctx, {"leak.py": 'KEY = "AKIAIOSFODNN7EXAMPLE"\n'})
    assert result.approved is False
    assert result.healing is not None
    assert any(v.category is ReviewCategory.SECRETS and not v.approved for v in result.verdicts)


async def test_council_approves_when_every_reviewer_approves(ctx: FactoryContext) -> None:
    anthropic_fake = _anthropic_of(ctx)
    for category in bug_council.REVIEWER_PROMPTS:
        anthropic_fake.register(ReviewVerdict, _approving(category))

    result = await bug_council.convene(ctx, CLEAN_SOURCES)
    assert result.approved is True
    assert result.healing is None


async def test_failed_reviewer_raises_council_unavailable_not_a_rejection(
    ctx: FactoryContext,
) -> None:
    """A review that could not run is not a verdict on the code -- it must never be
    silently folded into `approved=False` (which would spend a heal cycle
    regenerating code that was never actually reviewed). It raises instead, so the
    caller can route it like any other infrastructure failure.

    Every model reviewer is made to fail (3 retries each) so the assertion does not
    depend on which concurrent reviewer happens to pop which queued response.
    """
    anthropic_fake = _anthropic_of(ctx)
    outage = [
        ProviderUnavailable(Provider.ANTHROPIC, "503", status=503)
        for _ in range(3 * len(bug_council.REVIEWER_PROMPTS))
    ]
    anthropic_fake.register(ReviewVerdict, *outage)

    with pytest.raises(bug_council.CouncilUnavailable):
        await bug_council.convene(ctx, CLEAN_SOURCES)


async def test_empty_build_is_rejected(ctx: FactoryContext) -> None:
    result = await bug_council.convene(ctx, {})
    assert result.approved is False
    assert result.healing is not None


def test_healing_orders_by_severity() -> None:
    secrets = bug_council.scan_secrets({"a.py": 'KEY = "AKIAIOSFODNN7EXAMPLE"\n'})
    privilege = bug_council.scan_privilege({"b.sh": "sudo rm -rf /tmp/x\n"})
    healing = bug_council.build_healing(
        CouncilResult(approved=False, verdicts=[secrets, privilege])
    )
    assert healing.ordered_steps
    assert healing.ordered_steps[0].startswith("[critical]")


# --- deploy gate ------------------------------------------------------------


def test_deploy_refuses_unapproved_build() -> None:
    with pytest.raises(DeployNotPermitted):
        deploy_requires_approval(CouncilResult(approved=False, verdicts=[]))


def test_deploy_permits_approved_build() -> None:
    deploy_requires_approval(CouncilResult(approved=True, verdicts=[]))


def test_deploy_refuses_unapproved_build_with_no_council_at_all() -> None:
    """`council=None` is the shape `graph.py::deploy_accepting_risk` passes when
    there was never a real Council verdict to reference -- must fail exactly like
    an explicit rejection, not crash on a missing `.approved` attribute."""
    with pytest.raises(DeployNotPermitted):
        deploy_requires_approval(None)


def test_override_reason_bypasses_the_gate_even_with_no_council() -> None:
    """The one path allowed through without approval: a human's explicit override
    on their own product (see `hitl/interrupts.py`'s DEFER reply). `council=None`
    here is deliberate -- an override must never need a fabricated approved
    verdict to satisfy this check."""
    deploy_requires_approval(None, override_reason="MVP stage, fix before real launch")


def test_override_reason_bypasses_the_gate_even_over_an_explicit_rejection() -> None:
    deploy_requires_approval(
        CouncilResult(approved=False, verdicts=[]), override_reason="accepted risk"
    )


# --- generated path safety --------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["../../.env", "/etc/passwd", "../outside.txt", "sub/../../escape.txt"],
)
def test_generated_paths_cannot_escape_the_workspace(path: str, tmp_path) -> None:
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    with pytest.raises(UnsafeGeneratedPath):
        safe_target(root, path)


def test_generated_path_inside_workspace_is_allowed(tmp_path) -> None:
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    assert safe_target(root, "app/main.py").is_relative_to(root)


def test_read_workspace_excludes_installed_dependencies_from_review(settings) -> None:
    """.deps is where _verify_and_ship's install step puts dependencies so they
    survive into the separate test-run container (development/graph.py). Observed
    live: without this exclusion, Bug Council reviewed third-party library source
    as if it were the model's own code, and pytest/sqlalchemy/pydantic's legitimate
    internal eval()/exec()/shell=True usage tripped the privilege scanner and
    blocked every deploy."""
    root = settings.workspace_root / "product-1"
    (root / "app").mkdir(parents=True)
    (root / "app" / "main.py").write_text("print('hi')")
    deps_dir = root / ".deps" / "sqlalchemy" / "orm"
    deps_dir.mkdir(parents=True)
    (deps_dir / "_orm_constructors.py").write_text("eval('1')")

    sources = read_workspace("product-1", settings)

    assert "app/main.py" in sources
    assert not any(path.startswith(".deps") for path in sources)


def test_scaffold_plan_requires_files() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ScaffoldPlan(product_name="x", summary="y" * 30, files=[], test_command="pytest")


def _anthropic_of(ctx: FactoryContext) -> FakeProvider:
    provider = ctx.llm._registry.get(Provider.ANTHROPIC)
    assert isinstance(provider, FakeProvider)
    return provider


# --- cached shared prefix + stagger -----------------------------------------


def test_review_corpus_wraps_the_rendered_sources() -> None:
    corpus = bug_council.review_corpus("--- app.py ---\nprint(1)\n")
    assert corpus.startswith("The complete codebase under review follows.")
    assert "print(1)" in corpus
    assert corpus.endswith("Your specific review mandate follows.")


# --- honest truncation, not a silent drop -----------------------------------


def test_render_sources_reports_no_truncation_when_everything_fits() -> None:
    rendered, truncated = bug_council.render_sources({"app.py": "print(1)\n"})
    assert truncated is False
    assert "TRUNCATED" not in rendered


def test_render_sources_marks_truncation_and_names_the_dropped_files() -> None:
    sources = {"a.py": "x" * 40, "b.py": "y" * 40, "c.py": "z" * 40}
    rendered, truncated = bug_council.render_sources(sources, limit=50)

    assert truncated is True
    assert "TRUNCATED" in rendered
    # b.py was cut mid-content, c.py was never reached at all -- both named.
    assert "b.py" in rendered.split("TRUNCATED")[-1]
    assert "c.py" in rendered.split("TRUNCATED")[-1]


def test_review_corpus_never_claims_completeness_when_truncated() -> None:
    corpus = bug_council.review_corpus("--- app.py ---\nprint(1)\n", truncated=True)
    assert "complete codebase" not in corpus.lower()
    assert "too large to show in full" in corpus
    assert "withhold approval" in corpus


async def test_convene_logs_when_the_corpus_is_truncated(
    ctx: FactoryContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bug_council, "_MAX_REVIEW_CHARS", 10)
    anthropic_fake = _anthropic_of(ctx)
    for category in bug_council.REVIEWER_PROMPTS:
        anthropic_fake.register(ReviewVerdict, _approving(category))

    result = await bug_council.convene(ctx, {"app.py": "x" * 1000}, task_id=None)

    assert result.approved is True  # reviewers still ran; this only checks it didn't crash
    calls = anthropic_fake.calls
    assert any("TRUNCATED" in str(call.get("cache_prefix", "")) for call in calls)


async def test_the_corpus_is_not_in_the_prompt_it_is_the_cache_prefix(
    ctx: FactoryContext,
) -> None:
    """Moving the shared corpus into `cache_prefix` only helps if it actually left
    the per-call prompt — otherwise it would be billed on both sides."""
    anthropic_fake = _anthropic_of(ctx)
    for category in bug_council.REVIEWER_PROMPTS:
        anthropic_fake.register(ReviewVerdict, _approving(category))

    sources = {"app.py": "SECRET_MARKER_XYZ = 1\n"}
    await bug_council.convene(ctx, sources)

    for call in anthropic_fake.calls:
        assert "SECRET_MARKER_XYZ" not in call["prompt"]
        assert call["cache_prefix"] is not None
        assert "SECRET_MARKER_XYZ" in call["cache_prefix"]


async def test_every_reviewer_receives_the_byte_identical_cache_prefix(
    ctx: FactoryContext,
) -> None:
    """The whole point of a shared prefix is that it is shared — if `convene`
    recomputed it per reviewer instead of once, a stray difference would silently
    defeat the cache."""
    anthropic_fake = _anthropic_of(ctx)
    for category in bug_council.REVIEWER_PROMPTS:
        anthropic_fake.register(ReviewVerdict, _approving(category))

    await bug_council.convene(ctx, CLEAN_SOURCES)

    prefixes = {call["cache_prefix"] for call in anthropic_fake.calls}
    assert len(prefixes) == 1


async def test_the_first_reviewer_is_awaited_before_the_other_two_are_sent(
    ctx: FactoryContext,
) -> None:
    """Firing all three concurrently means the shared prefix's cache write from
    reviewer 1 has not landed before 2 and 3 are sent, so both would miss and pay a
    write premium instead of a read discount. Staggering is what makes the cache
    change a saving rather than a ~25% regression."""
    anthropic_fake = _anthropic_of(ctx)
    for category in bug_council.REVIEWER_PROMPTS:
        anthropic_fake.register(ReviewVerdict, _approving(category))

    await bug_council.convene(ctx, CLEAN_SOURCES)

    first_category = next(iter(bug_council.REVIEWER_PROMPTS))
    assert anthropic_fake.calls[0]["system"] == bug_council.REVIEWER_PROMPTS[first_category]
