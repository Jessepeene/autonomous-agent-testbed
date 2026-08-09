"""The deploy gate: deterministic scanners, unanimity, and healing instructions."""

from __future__ import annotations

import pytest

from factory.engines.development import bug_council
from factory.engines.development.deploy import DeployNotPermitted, deploy_requires_approval
from factory.engines.development.scaffold.generator import UnsafeGeneratedPath, safe_target
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


async def test_failed_reviewer_blocks_the_deploy(ctx: FactoryContext) -> None:
    """A review that could not run has not approved anything.

    Every model reviewer is made to fail (3 retries each) so the assertion does not
    depend on which concurrent reviewer happens to pop which queued response.
    """
    anthropic_fake = _anthropic_of(ctx)
    outage = [
        ProviderUnavailable(Provider.ANTHROPIC, "503", status=503)
        for _ in range(3 * len(bug_council.REVIEWER_PROMPTS))
    ]
    anthropic_fake.register(ReviewVerdict, *outage)

    result = await bug_council.convene(ctx, CLEAN_SOURCES)
    assert result.approved is False
    assert result.healing is not None
    assert all(
        not v.approved for v in result.verdicts if v.category in bug_council.REVIEWER_PROMPTS
    )


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
