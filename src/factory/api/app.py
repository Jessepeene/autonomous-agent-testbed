"""HTTP surface.

Deliberately small: health, readiness, metrics, the Stripe webhook, and the
unsubscribe endpoint. The factory is driven by its own loop, not by requests — this
is an observation and ingest surface, not a control plane.
"""

from __future__ import annotations

import hmac
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
async def metrics_endpoint(
    authorization: str = Header(default="", alias="Authorization"),
) -> dict[str, Any]:
    """Full financial and operational state — spend, balance, task counts.

    Was unauthenticated and publicly reachable. Two independent layers now: the
    `Caddyfile` 404s this path from the public interface, and this bearer check
    stands behind it so a proxy misconfiguration cannot silently re-expose it.

    Fails closed when no token is configured: an unset credential means "nobody may
    read this", never "everybody may". Operators read it over the loopback bind
    (`127.0.0.1:8000`, see docker-compose.yml) with the token set.
    """
    expected = get_settings().secret("METRICS_AUTH_TOKEN")
    if not expected:
        raise HTTPException(status_code=503, detail="METRICS_AUTH_TOKEN is not configured")
    scheme, _, presented = authorization.partition(" ")
    # compare_digest on both halves: a plain `!=` on the token leaks length and
    # prefix through timing, and this endpoint is reachable from the container
    # network even with Caddy refusing it at the edge.
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip(), expected):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")
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
    #: Set only by a page currently running a live AB test -- see the variant-split
    #: codegen pattern in scaffold/generator.py. Unvalidated against a currently-
    #: running test here deliberately: the beacon must never 500 just because a
    #: test concluded between page load and this submission landing.
    variant: str | None = Field(default=None, max_length=32)


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
            variant=submission.variant,
        )
    return {"received": True}


class PageViewSubmission(BaseModel):
    #: Free text from the visitor's browser, not validated as a real URL/referrer --
    #: this is best-effort growth signal, never treated as authoritative for
    #: anything security-sensitive. Bounded the same way InterestSubmission's
    #: fields are, so an oversized payload cannot be used to bloat the table.
    path: str = Field(min_length=1, max_length=400)
    referrer: str | None = Field(default=None, max_length=400)
    #: Set only by a product currently running a live AB test -- see the
    #: variant-split codegen pattern. Unvalidated against any known-running test
    #: here deliberately: the beacon must never 500 just because a test concluded
    #: between page load and this request landing.
    variant: str | None = Field(default=None, max_length=32)


@app.post("/analytics/{product_id}")
async def analytics(product_id: str, submission: PageViewSubmission) -> dict[str, Any]:
    """Page-view beacon for a product's own generated frontend.

    The same shape and posture as `/interest/{product_id}` above -- an
    unauthenticated public write endpoint a generated page's own inline script
    calls -- extended to traffic volume rather than expressed interest. No
    third-party analytics dependency: this factory fully controls the schema and
    the query surface that reads it back (see `repository.tally_page_views`,
    used by the growth-analyst role).

    Anonymous by design, unlike `/interest`: no email, no dedup, no identity. A
    flood of fake views is a data-quality problem for growth decisions, not a
    privacy or spend-gating one the way a fake interest signal would be — so
    unlike `/interest`, nothing here needs to be load-bearing against abuse
    beyond the field-length bounds already enforced by the schema.
    """
    async with session_scope() as session:
        product = await repository.get_product(session, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="unknown product")
        await repository.record_page_view(
            session,
            product_id=product_id,
            path=submission.path,
            referrer=submission.referrer,
            variant=submission.variant,
        )
    return {"received": True}
