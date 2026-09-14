"""HTTP surface.

Deliberately small: health, readiness, metrics, the Stripe webhook, and the
unsubscribe endpoint. The factory is driven by its own loop, not by requests — this
is an observation and ingest surface, not a control plane.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from factory.api import abuse
from factory.api import dashboard as dashboard_api
from factory.api.auth import require_bearer_token
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
    configure_logging(settings.log_level, file_path=settings.log_file_path)
    app.state.report = run_preflight(settings)
    log.info("api.started", dry_run=settings.factory_dry_run)
    yield


app = FastAPI(title="Autonomous SaaS Factory", version="0.1.0", lifespan=lifespan)

# Every landing page this factory ships is served from a `*.vercel.app` origin and
# posts to `/interest/{product_id}` here, which makes every submission a
# cross-origin request. There was no CORS configuration at all, so the browser's
# preflight `OPTIONS` got a bare 405 and the fetch never happened -- the form failed
# for every visitor of every page ever deployed, and the interest signal the whole
# validation stage is built on could not arrive from a browser. Nothing caught it
# because nothing in the pipeline has ever opened a deployed page: every reviewer
# reads source, and `curl` against the endpoint succeeds exactly as it always did.
#
# This middleware is app-wide -- Starlette has no per-route form -- so the grants
# below are deliberately the narrowest that make the two beacons work:
#
# - `allow_methods` is POST and OPTIONS only. No GET is ever approved for a
#   cross-origin read, so `/dashboard/*`, `/metrics` and `/health` gain nothing
#   from this. The two POST beacons are the only POSTs a browser can reach; every
#   other write is bearer-authenticated or signature-verified.
# - `allow_credentials=False`, so no cookie or auth header rides along. A hostile
#   page still cannot make an authenticated request, because bearer tokens are not
#   attached by the browser the way cookies are.
# - `allow_origins=["*"]` because the origin genuinely cannot be known in advance:
#   each product deploys to a fresh random `*.vercel.app` subdomain, and a custom
#   domain later replaces it. Abuse is bounded where it should be -- `abuse
#   .rate_limit` per IP and per product -- not by guessing origins.
#
# `/dashboard/*` and `/metrics` are additionally refused outright at Caddy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    max_age=86_400,
)
app.include_router(dashboard_api.router)


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


@app.get("/metrics", dependencies=[Depends(require_bearer_token("metrics_auth_token"))])
async def metrics_endpoint() -> dict[str, Any]:
    """Full financial and operational state — spend, balance, task counts.

    Was unauthenticated and publicly reachable. Two independent layers now: the
    `Caddyfile` 404s this path from the public interface, and the bearer check
    (see `api/auth.py::require_bearer_token`) stands behind it so a proxy
    misconfiguration cannot silently re-expose it.

    Operators read it over the loopback bind (`127.0.0.1:8000`, see
    docker-compose.yml) with the token set.
    """
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
    """One-click unsubscribe and erasure.

    Resolves the token to the address it was issued for and tombstones that address
    into the blacklist, so the suppression outlives the database row.

    Tries an outreach `unsubscribe_token` first, then a validation-signal
    `confirmation_token` -- the two are unrelated tables, and a landing-page
    visitor who only ever submitted `/interest` (never received outreach) had no
    self-serve erasure route at all before this, only `POST /interest` and the
    confirmation link, neither of which deletes anything.
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
            email = await repository.validation_signal_email_for_token(session, token)
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


@app.get(
    "/confirm/{token}",
    dependencies=[
        Depends(abuse.rate_limit(scope="confirm", per_ip_setting="interest_rate_limit_per_ip_hour"))
    ],
)
async def confirm_interest(token: str) -> Response:
    """The double opt-in click. Until this fires, a validation signal is an
    unverified claim, not evidence -- see `ValidationSignal.confirmed_at` and
    `_evaluate_validation`'s `tally_validation_signals(confirmed_only=True)`.

    Rate-limited per IP: this is as unauthenticated as `/interest` itself
    (anyone who guesses or receives a token can hit it), just far less
    valuable to spam since it can only ever confirm rows that already exist.
    """
    async with session_scope() as session:
        email = await repository.confirm_validation_signal(session, token)
    if email is None:
        raise HTTPException(status_code=404, detail="unknown or expired confirmation link")
    return Response(
        content=(
            "Thanks — your interest has been confirmed. If you'd rather not be "
            f"contacted about this again, visit /u/{token} to have your details deleted."
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


@app.post(
    "/interest/{product_id}",
    dependencies=[
        Depends(
            abuse.rate_limit(
                scope="interest",
                per_ip_setting="interest_rate_limit_per_ip_hour",
                per_product_setting="interest_rate_limit_per_product_hour",
            )
        )
    ],
)
async def interest(
    product_id: str, submission: InterestSubmission, request: Request
) -> dict[str, Any]:
    """Interest capture for a validation-stage landing page.

    The first unauthenticated public write endpoint in this factory -- every other
    one is either read-only or signature-verified (Stripe/Mailgun webhooks). It
    directly feeds a spend-gating decision (`EVALUATE_VALIDATION`), so dedup by
    email is load-bearing, not optional: `insert_validation_signal` is a unique
    `(product_id, email)` upsert, so repeat submissions from the same address
    (accidental or hostile) can never inflate the count. Always returns success —
    whether the row was new or a duplicate is not information this endpoint should
    leak back to the caller.

    A row only *counts* toward that gate once its confirmation link is clicked
    (`GET /confirm/{token}`) -- proven necessary live, when six unconfirmed
    submissions alone cleared the response-rate bar and carried a product to a
    real build. Skipped only under `factory_dry_run`, where there is no real
    inbox to click a link from; the row is still recorded, just auto-confirmed
    at insert time (see `insert_validation_signal`), so the rest of the
    pipeline never needs a dry-run-aware branch.
    """
    settings = get_settings()
    require_confirmation = (
        settings.validation_require_double_opt_in and not settings.factory_dry_run
    )
    confirmation_token = secrets.token_urlsafe(32) if require_confirmation else None
    ip_hash = abuse.hash_ip(request, settings)

    async with session_scope(settings) as session:
        product = await repository.get_product(session, product_id)
        if product is None:
            # validation_signals.product_id is a real FK -- a nonexistent id would
            # otherwise 500 on the INSERT rather than giving the caller (or a
            # probing scanner) a clean, unsurprising response.
            raise HTTPException(status_code=404, detail="unknown product")
        signal_id = await repository.insert_validation_signal(
            session,
            product_id=product_id,
            email=submission.email,
            message=submission.message,
            variant=submission.variant,
            require_confirmation=require_confirmation,
            confirmation_token=confirmation_token,
            submitter_ip_hash=ip_hash,
        )

    # Only a genuinely new row needs a confirmation email -- a duplicate
    # submission from an address that already has one either has an unclicked
    # link outstanding (resending would just create a second live token for
    # the same row, which the schema doesn't have room for) or is already
    # confirmed. `send_email` (not raw SMTP) inherits the fail-closed
    # blacklist gate, so an erased address can never receive one either.
    if require_confirmation and signal_id is not None and confirmation_token is not None:
        from factory.engines.marketing.email import send_email

        base = (settings.unsubscribe_base_url or "").rstrip("/")
        confirm_link = f"{base}/confirm/{confirmation_token}" if base else None
        if confirm_link:
            await send_email(
                to=submission.email,
                subject="Please confirm your interest",
                body=(
                    "Thanks for your interest — please confirm by visiting the link "
                    f"below. If you didn't request this, you can ignore it.\n\n{confirm_link}"
                ),
                settings=settings,
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


@app.post(
    "/analytics/{product_id}",
    dependencies=[
        Depends(
            abuse.rate_limit(scope="analytics", per_ip_setting="analytics_rate_limit_per_ip_hour")
        )
    ],
)
async def analytics(product_id: str, submission: PageViewSubmission) -> dict[str, Any]:
    """Page-view beacon for a product's own generated frontend.

    The same shape and posture as `/interest/{product_id}` above -- an
    unauthenticated public write endpoint a generated page's own inline script
    calls -- extended to traffic volume rather than expressed interest. No
    third-party analytics dependency: this factory fully controls the schema and
    the query surface that reads it back (see `repository.tally_page_views`,
    used by the growth-analyst role).

    Anonymous by design, unlike `/interest`: no email, no dedup, no identity,
    and no spend-gating decision reads this table directly. A rate limit still
    applies -- fake volume is a data-quality problem for `EVALUATE_AB_TEST` and
    GROWTH_ANALYST, not a privacy one, but it is still a real input worth
    capping -- looser than `/interest`'s, and per-IP only (no per-product cap,
    matching that a genuinely popular page should be free to get a lot of
    real traffic from a lot of different visitors).
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
