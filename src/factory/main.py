"""Factory entrypoint.

Boot order matters: preflight decides which engines may run *before* anything is
constructed, so an unconfigured engine is never wired into the graph at all rather
than failing on its first task.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import uuid

from factory.hitl.telegram import build_notifier
from factory.llm.client import UsageRecord, UsageRecorder, build_client
from factory.logging import configure_logging, get_logger
from factory.observability.costs import estimate_usd
from factory.preflight import PreflightReport, run_preflight, write_missing_credentials
from factory.sandbox.runner import get_sandbox
from factory.settings import EngineName, Settings, get_settings
from factory.state import repository
from factory.state.checkpointer import postgres_checkpointer
from factory.state.db import dispose_engine, session_scope
from factory.supervisor.context import FactoryContext
from factory.supervisor.graph import build_graph
from factory.supervisor.scheduler import Scheduler

log = get_logger(__name__)


def _usage_recorder(settings: Settings) -> UsageRecorder:
    async def record(entry: UsageRecord) -> None:
        cost_usd = estimate_usd(
            entry.model,
            entry.input_tokens,
            entry.output_tokens,
            entry.cache_read_tokens,
            entry.cache_write_tokens,
        )
        try:
            async with session_scope(settings) as session:
                await repository.record_llm_call(
                    session,
                    provider=entry.provider,
                    model=entry.model,
                    task_type=entry.task_type,
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    cache_read_tokens=entry.cache_read_tokens,
                    cache_write_tokens=entry.cache_write_tokens,
                    latency_ms=entry.latency_ms,
                    outcome=entry.outcome,
                    engine=entry.engine,
                    task_id=entry.task_id,
                    cost_usd=cost_usd,
                    tunable_parameter_id=entry.tunable_parameter_id,
                )
        except Exception:
            log.warning("usage.record_failed", provider=entry.provider, model=entry.model)

    return record


def build_context(settings: Settings, report: PreflightReport) -> FactoryContext:
    enabled = {e for e in EngineName if e not in report.disabled_engines}
    enabled.add(EngineName.SUPERVISOR)
    return FactoryContext(
        settings=settings,
        llm=build_client(settings, usage_recorder=_usage_recorder(settings)),
        notifier=build_notifier(settings),
        sandbox=get_sandbox(settings),
        enabled_engines=enabled,
        docker_available=report.docker_available,
        worker_id=f"supervisor-{uuid.uuid4().hex[:8]}",
        disabled_reasons={k: v for k, v in report.disabled_engines.items()},
    )


async def _announce_degradations(ctx: FactoryContext, report: PreflightReport) -> None:
    """Queue a MISSING_CREDENTIAL interrupt for each degradation.

    Queued, not sent: if Telegram itself is the missing credential the row waits in
    Postgres and is delivered the moment a token appears.
    """
    from factory.hitl import interrupts
    from factory.state.schemas import InterruptReason

    async with session_scope(ctx.settings) as session:
        for finding in report.degradations:
            await interrupts.request(
                session,
                reason=InterruptReason.MISSING_CREDENTIAL,
                subject=f"{finding.subject} is not configured",
                detail=f"{finding.detail}\n\nFix: {finding.remedy}",
            )


async def _serve_api(settings: Settings, stop: asyncio.Event) -> None:
    """Run the FastAPI surface on the same event loop as the supervisor."""
    import uvicorn

    from factory.api.app import app

    config = uvicorn.Config(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_config=None,  # structlog owns logging
        access_log=False,
    )
    server = uvicorn.Server(config)

    async def _shutdown() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(_shutdown())
    try:
        await server.serve()
    finally:
        watcher.cancel()


async def run_factory(*, once: bool = False) -> int:
    settings = get_settings()
    configure_logging(settings.log_level)

    report = run_preflight(settings)
    write_missing_credentials(report)
    if not report.ok:
        log.error(
            "factory.blocked",
            blocks=[f.subject for f in report.hard_blocks],
            hint="see MISSING_CREDENTIALS.md",
        )
        return 2

    ctx = build_context(settings, report)
    log.info(
        "factory.boot",
        worker=ctx.worker_id,
        dry_run=ctx.dry_run,
        docker=ctx.docker_available,
        enabled=[str(e) for e in sorted(ctx.enabled_engines)],
        disabled={str(k): v for k, v in ctx.disabled_reasons.items()},
    )

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    try:
        await _announce_degradations(ctx, report)
        async with postgres_checkpointer(settings) as checkpointer:
            graph = build_graph(ctx, checkpointer)
            scheduler = Scheduler(ctx, graph)
            try:
                if once:
                    result = await scheduler.tick()
                    print(json.dumps(result, default=str, indent=2))
                else:
                    # The loop and the HTTP surface share one process and one event
                    # loop. Without the server the Stripe webhook is unreachable, so
                    # revenue never lands and the container never reports healthy.
                    await asyncio.gather(
                        scheduler.run_forever(stop),
                        _serve_api(settings, stop),
                    )
            finally:
                await scheduler.release_all_leases()
    finally:
        await dispose_engine()
    return 0


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        with contextlib.suppress(NotImplementedError):
            # Windows' proactor loop does not implement add_signal_handler; the
            # default KeyboardInterrupt path covers Ctrl+C there.
            loop.add_signal_handler(sig, stop.set)


def cli() -> int:
    parser = argparse.ArgumentParser(prog="factory", description="Autonomous SaaS factory")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single supervisor tick and exit (used by the dry-run check).",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(run_factory(once=args.once))
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(cli())
