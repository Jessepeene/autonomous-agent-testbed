"""HTTP surface.

Deliberately small: health, readiness, metrics, the Stripe webhook, and the
unsubscribe endpoint. The factory is driven by its own loop, not by requests — this
is an observation and ingest surface, not a control plane.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from factory.engines.finance import graph as finance_graph
from factory.engines.finance.webhooks import SignatureInvalid, process_event, verify
from factory.engines.marketing import deliverability
from factory.logging import configure_logging, get_logger
from factory.observability import metrics
from factory.preflight import run_preflight
from factory.settings import EngineName, get_settings
from factory.state import repository
from factory.state.db import session_scope

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    app.state.report = run_preflight(settings)
    log.info("api.started", dry_run=settings.factory_dry_run)
    yield


app = FastAPI(title="Autonomous SaaS Factory", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness plus the exact reason each disabled engine is disabled."""
    report = getattr(app.state, "report", None) or run_preflight()
    return {
        "status": "ok" if report.ok else "blocked",
        "dry_run": get_settings().factory_dry_run,
        "docker": report.docker_available,
        "engines": {
            str(engine): {
                "enabled": engine not in report.disabled_engines,
                "reason": report.disabled_engines.get(engine),
            }
            for engine in EngineName
        },
    }


@app.get("/ready")
async def ready() -> dict[str, Any]:
    """Readiness depends on the database, which is the one hard dependency."""
    try:
        async with session_scope() as session:
            await repository.count_pending_tasks(session)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return {"status": "ready"}


@app.get("/metrics")
async def metrics_endpoint() -> dict[str, Any]:
    async with session_scope() as session:
        snapshot = await metrics.collect(session)
    return snapshot.as_dict()


@app.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(default="", alias="Stripe-Signature"),
) -> dict[str, Any]:
    payload = await request.body()
    try:
        event = verify(payload, stripe_signature)
    except SignatureInvalid as exc:
        # Never process an unverified money event.
        log.warning("stripe.signature_invalid", detail=str(exc)[:200])
        raise HTTPException(status_code=400, detail="invalid signature") from exc

    async with session_scope() as session:
        outcome = await process_event(session, event)
        if outcome.handled:
            bucket = str(event.get("created", "0"))[:8]
            await finance_graph.schedule_reconciliation(session, bucket)

    return {"handled": outcome.handled, "kind": outcome.kind, "detail": outcome.detail}


@app.post("/webhooks/mailgun")
async def mailgun_webhook(request: Request) -> dict[str, Any]:
    """Bounce/complaint feedback for the spam-complaint circuit breaker.

    Mailgun puts its signature in the JSON body, not a header (see
    `deliverability.verify`), so the whole parsed payload is handed to it rather
    than a header value.
    """
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")

    try:
        deliverability.verify(payload)
    except deliverability.SignatureInvalid as exc:
        # Never process an unverified deliverability event — a forged "complained"
        # event could be used to freeze a competitor's marketing engine, or a forged
        # absence of complaints could hide a real one.
        log.warning("mailgun.signature_invalid", detail=str(exc)[:200])
        raise HTTPException(status_code=400, detail="invalid signature") from exc

    async with session_scope() as session:
        outcome = await deliverability.process_event(session, payload)

    return {"handled": outcome.handled, "event": outcome.event, "detail": outcome.detail}


@app.get("/u/{token}")
async def unsubscribe(token: str) -> Response:
    """One-click unsubscribe.

    Resolves the token to the address it was issued for and tombstones that address
    into the blacklist, so the suppression outlives the database row.
    """
    from sqlalchemy import select

    from factory.state.orm import Lead, OutreachLog

    async with session_scope() as session:
        stmt = (
            select(Lead.work_email)
            .join(OutreachLog, OutreachLog.lead_id == Lead.id)
            .where(OutreachLog.unsubscribe_token == token)
            .limit(1)
        )
        email = (await session.execute(stmt)).scalar_one_or_none()
        if email is None:
            raise HTTPException(status_code=404, detail="unknown unsubscribe token")
        await repository.erase_lead(session, str(email))
        # Same address may also have submitted interest on a validation landing
        # page -- a separate table, but the same GDPR erasure obligation.
        await repository.erase_validation_signals(session, str(email))

    return Response(
        content=(
            "You have been unsubscribed and your details have been deleted. "
            "This address will not be contacted again."
        ),
        media_type="text/plain",
    )


class InterestSubmission(BaseModel):
    #: No `pydantic[email]`/`email-validator` dependency in this project (see
    #: `fisher_exact_less` using stdlib `math.comb` for the same reason) -- a loose
    #: length/shape check is enough here since this only gates a build-or-scrap
    #: decision, never sends anything to the address.
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    message: str | None = Field(default=None, max_length=2000)


@app.post("/interest/{product_id}")
async def interest(product_id: str, submission: InterestSubmission) -> dict[str, Any]:
    """Interest capture for a validation-stage landing page.

    The first unauthenticated public write endpoint in this factory -- every other
    one is either read-only or signature-verified (Stripe/Mailgun webhooks). It
    directly feeds a spend-gating decision (`EVALUATE_VALIDATION`), so dedup by
    email is load-bearing, not optional: `insert_validation_signal` is a unique
    `(product_id, email)` upsert, so repeat submissions from the same address
    (accidental or hostile) can never inflate the count. Always returns success —
    whether the row was new or a duplicate is not information this endpoint should
    leak back to the caller.
    """
    async with session_scope() as session:
        product = await repository.get_product(session, product_id)
        if product is None:
            # validation_signals.product_id is a real FK -- a nonexistent id would
            # otherwise 500 on the INSERT rather than giving the caller (or a
            # probing scanner) a clean, unsurprising response.
            raise HTTPException(status_code=404, detail="unknown product")
        await repository.insert_validation_signal(
            session,
            product_id=product_id,
            email=submission.email,
            message=submission.message,
        )
    return {"received": True}
