"""Boot-time capability and credential audit.

Two severities, and the difference matters:

* ``HARD_BLOCK`` — the factory cannot function at all. Exit 2 with a written remedy.
* ``DEGRADES``   — one or more engines cannot run. The factory boots anyway and the
  Supervisor routes work to the engines that *can* run (the never-idle rule). A
  ``MISSING_CREDENTIAL`` interrupt is queued and flushes once Telegram exists.

This resolves the bootstrap paradox: the only human channel is Telegram, but
Telegram itself needs a token, so a missing credential can never be *reported*
through the normal escalation path. It is written to disk instead.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from factory.logging import configure_logging, get_logger
from factory.settings import REPO_ROOT, EngineName, Settings, get_settings

log = get_logger(__name__)


class Severity(StrEnum):
    HARD_BLOCK = "hard_block"
    DEGRADES = "degrades"


@dataclass(frozen=True, slots=True)
class CredentialSpec:
    env_var: str
    severity: Severity
    purpose: str
    remedy: str
    engines: tuple[EngineName, ...] = ()


CREDENTIALS: tuple[CredentialSpec, ...] = (
    CredentialSpec(
        env_var="DATABASE_URL",
        severity=Severity.HARD_BLOCK,
        purpose="All persistent state and LangGraph checkpoints.",
        remedy=(
            "Run `docker compose up -d postgres` and use "
            "postgresql+asyncpg://factory:factory@localhost:5432/factory, "
            "or point DATABASE_URL at a hosted Postgres (Neon, Supabase)."
        ),
    ),
    CredentialSpec(
        env_var="GEMINI_API_KEY",
        severity=Severity.DEGRADES,
        purpose="General reasoning: orchestration, discovery, compliance, marketing copy.",
        remedy="Create a key at https://aistudio.google.com/apikey and set GEMINI_API_KEY.",
        engines=(EngineName.DISCOVERY, EngineName.COMPLIANCE, EngineName.MARKETING),
    ),
    CredentialSpec(
        env_var="ANTHROPIC_API_KEY",
        severity=Severity.DEGRADES,
        purpose="Code generation and the Bug Council.",
        remedy="Create a key at https://console.anthropic.com and set ANTHROPIC_API_KEY.",
        engines=(EngineName.DEVELOPMENT,),
    ),
    CredentialSpec(
        env_var="TELEGRAM_BOT_TOKEN",
        severity=Severity.DEGRADES,
        purpose="The only human-in-the-loop escalation channel.",
        remedy="Talk to @BotFather, then set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.",
    ),
    CredentialSpec(
        env_var="STRIPE_API_KEY",
        severity=Severity.DEGRADES,
        purpose="Revenue ingest, reinvestment, virtual card limits.",
        remedy="Set STRIPE_API_KEY and STRIPE_WEBHOOK_SECRET from the Stripe dashboard.",
        engines=(EngineName.FINANCE,),
    ),
    CredentialSpec(
        env_var="VERCEL_TOKEN",
        severity=Severity.DEGRADES,
        purpose="MVP deployment target.",
        remedy="Set VERCEL_TOKEN (or CLOUDFLARE_API_TOKEN) to enable automated deploys.",
        engines=(EngineName.DEVELOPMENT,),
    ),
    CredentialSpec(
        env_var="MAILGUN_API_KEY",
        severity=Severity.DEGRADES,
        purpose="Outbound email transport for cold outreach (deliverability API, not raw SMTP).",
        remedy=(
            "Set MAILGUN_API_KEY, MAILGUN_DOMAIN, MAILGUN_WEBHOOK_SIGNING_KEY and "
            "OUTBOUND_FROM_ADDRESS."
        ),
        engines=(EngineName.MARKETING,),
    ),
)


@dataclass(slots=True)
class Finding:
    subject: str
    severity: Severity
    detail: str
    remedy: str


@dataclass(slots=True)
class PreflightReport:
    findings: list[Finding] = field(default_factory=list)
    docker_available: bool = False
    disabled_engines: dict[EngineName, str] = field(default_factory=dict)

    @property
    def hard_blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.HARD_BLOCK]

    @property
    def degradations(self) -> list[Finding]:
        return [f for f in self.findings if f.severity is Severity.DEGRADES]

    @property
    def ok(self) -> bool:
        return not self.hard_blocks

    def enabled_engines(self) -> list[EngineName]:
        return [e for e in EngineName if e not in self.disabled_engines]


def check_sandbox_workspace_sharing(settings: Settings) -> Finding | None:
    """Prove the factory process and its sandbox containers actually share a
    filesystem, rather than assuming it from configuration.

    `docker-compose.yml`'s sibling-container pattern (the factory drives the *host*
    Docker daemon via a mounted socket, rather than running Docker-in-Docker) has a
    sharp edge: a `docker run --volume SRC:DST` issued from inside a container is
    resolved by the daemon against *its own* filesystem view — the host's — not the
    calling container's. If `WORKSPACE_ROOT` is not bind-mounted at the identical path
    on the host and inside the factory container (a named volume, or two different
    paths, both silently fail this), a sandbox container gets an empty `/workspace`
    while the factory's own view of the same nominal path has the real generated
    code. Nothing raises: `pip install` runs against a missing `requirements.txt`,
    the test command finds no tests, and every build heals and eventually escalates
    as if the *generated code* were always broken. A mocked test environment can
    never catch this — it never launches two real, separate containers.

    Cheap to check directly: write a marker from inside a real sandbox container and
    read it back from this process. If they don't see the same file, no further
    inference is needed.
    """
    probe_dir = settings.workspace_root / ".preflight-probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    marker = probe_dir / "marker"
    marker.unlink(missing_ok=True)

    # Resolved from PATH by design, same as docker_available() below.
    probe_argv = [
        "docker",
        "run",
        "--rm",
        "--volume",
        f"{probe_dir}:/probe",
        settings.sandbox_image,
        "sh",
        "-c",
        "echo ok > /probe/marker",
    ]
    try:
        subprocess.run(  # noqa: S603 - probe_argv is built entirely from config, not input
            probe_argv,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Finding(
            subject="SANDBOX_WORKSPACE_SHARING",
            severity=Severity.DEGRADES,
            detail=f"Could not run a sandbox probe container to verify workspace sharing: {exc}",
            remedy="Confirm `docker run` works manually with the configured SANDBOX_IMAGE.",
        )

    shared = marker.exists() and marker.read_text(encoding="utf-8").strip() == "ok"
    marker.unlink(missing_ok=True)
    if shared:
        return None
    return Finding(
        subject="SANDBOX_WORKSPACE_SHARING",
        severity=Severity.DEGRADES,
        detail=(
            f"A sandbox container wrote a marker into {probe_dir}, but this process "
            "cannot see it. WORKSPACE_ROOT is not bind-mounted at the same path on the "
            "host and inside the factory container. Every Development build will see "
            "an empty workspace and fail every generated test until this is fixed — "
            "indistinguishable from the generated code itself always being broken."
        ),
        remedy=(
            "Bind-mount WORKSPACE_ROOT at the *identical* path on the host and in the "
            "factory container (see docker-compose.yml and the 'Sandboxing note' in "
            "DEPLOYMENT.md) — not a named volume, and not two different paths."
        ),
    )


def docker_available() -> bool:
    """True only when a docker daemon actually answers.

    `docker` being on PATH is not enough — a CLI with no reachable daemon would let
    the Development Engine start work it can never sandbox.
    """
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603 - argv is a hardcoded literal, no untrusted input
            # Resolved from PATH by design: Docker's install location varies per OS.
            ["docker", "info", "--format", "{{.ServerVersion}}"],  # noqa: S607
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def run_preflight(settings: Settings | None = None) -> PreflightReport:
    settings = settings or get_settings()
    report = PreflightReport()

    for spec in CREDENTIALS:
        if settings.secret(spec.env_var) is not None:
            continue
        report.findings.append(
            Finding(
                subject=spec.env_var,
                severity=spec.severity,
                detail=spec.purpose,
                remedy=spec.remedy,
            )
        )
        for engine in spec.engines:
            report.disabled_engines.setdefault(engine, f"{spec.env_var} is not set")

    # At least one provider key is required; neither alone is individually mandatory.
    if not settings.has_gemini and not settings.has_anthropic:
        report.findings.append(
            Finding(
                subject="LLM_PROVIDER",
                severity=Severity.HARD_BLOCK,
                detail="No model provider is configured; every engine needs at least one.",
                remedy="Set GEMINI_API_KEY or ANTHROPIC_API_KEY (both is strongly preferred).",
            )
        )

    report.docker_available = docker_available()
    if not report.docker_available:
        report.findings.append(
            Finding(
                subject="DOCKER",
                severity=Severity.DEGRADES,
                detail=(
                    "No reachable Docker daemon. Generated code can only run inside a "
                    "sandbox, so the Development Engine stays disabled rather than "
                    "executing anything on the host."
                ),
                remedy="Install Docker Desktop (Windows) or docker-ce (VPS) and start the daemon.",
            )
        )
        report.disabled_engines[EngineName.DEVELOPMENT] = "no Docker daemon"
    else:
        sharing_finding = check_sandbox_workspace_sharing(settings)
        if sharing_finding is not None:
            report.findings.append(sharing_finding)
            report.disabled_engines[EngineName.DEVELOPMENT] = (
                "sandbox workspace is not correctly shared (see SANDBOX_WORKSPACE_SHARING)"
            )

    if not settings.blacklist_path.exists():
        report.findings.append(
            Finding(
                subject="BLACKLIST",
                severity=Severity.HARD_BLOCK,
                detail=(
                    f"{settings.blacklist_path.name} is missing. Outbound email fails "
                    "closed without it, and a silently absent suppression list is a "
                    "compliance incident waiting to happen."
                ),
                remedy=f"Restore {settings.blacklist_path} from version control.",
            )
        )

    return report


def write_missing_credentials(report: PreflightReport, path: Path | None = None) -> Path | None:
    """Persist the audit where a human can read it without a working Telegram bot."""
    target = path or (REPO_ROOT / "MISSING_CREDENTIALS.md")
    if report.ok and not report.degradations:
        target.unlink(missing_ok=True)
        return None

    lines = [
        "# Missing configuration",
        "",
        "Written by `factory.preflight`. This file is regenerated on every boot and is",
        "git-ignored. Delete nothing by hand — fix the cause and reboot.",
        "",
    ]
    if report.hard_blocks:
        lines += ["## Blocking — the factory will not start", ""]
        for f in report.hard_blocks:
            lines += [f"### {f.subject}", "", f.detail, "", f"**Fix:** {f.remedy}", ""]
    if report.degradations:
        lines += ["## Degrading — the factory runs with reduced capability", ""]
        for f in report.degradations:
            lines += [f"### {f.subject}", "", f.detail, "", f"**Fix:** {f.remedy}", ""]
    if report.disabled_engines:
        lines += ["## Engines currently disabled", ""]
        lines += [f"- `{engine}` — {reason}" for engine, reason in report.disabled_engines.items()]
        lines += [""]

    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def cli() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, pretty=True)
    report = run_preflight(settings)
    written = write_missing_credentials(report)

    for finding in report.findings:
        log.warning(
            "preflight.finding",
            subject=finding.subject,
            severity=str(finding.severity),
            detail=finding.detail,
        )
    log.info(
        "preflight.summary",
        ok=report.ok,
        docker=report.docker_available,
        enabled_engines=[str(e) for e in report.enabled_engines()],
        disabled_engines={str(k): v for k, v in report.disabled_engines.items()},
        report_written=str(written) if written else None,
    )
    if not report.ok:
        log.error("preflight.blocked", blocks=[f.subject for f in report.hard_blocks])
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
