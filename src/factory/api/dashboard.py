"""GET /dashboard/state -- the read-only API behind the operator dashboard.

Same posture as GET /metrics (api/app.py): bearer-token gated. Also carries
one write action, `POST /dashboard/interrupts/{id}/resolve` -- deliberately
narrow: it answers an open escalation exactly the way a Telegram reply would
(same `hitl.interrupts.resolve_reply`), nothing more. A separate router
module rather than routes straight in app.py because this endpoint has real
internal structure (see observability/dashboard.py) that would otherwise
crowd that file's deliberately small surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from factory.api.auth import require_bearer_token
from factory.config_writer import InvalidEnvValue, write_env_value
from factory.hitl import interrupts as hitl_interrupts
from factory.observability import dashboard as dashboard_metrics
from factory.observability import integrations as integrations_metrics
from factory.preflight import run_preflight
from factory.settings import EngineName, get_settings
from factory.state import repository
from factory.state.db import session_scope
from factory.state.orm import Interrupt, Product
from factory.state.schemas import TaskKind

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/state", dependencies=[Depends(require_bearer_token("dashboard_auth_token"))])
async def dashboard_state(request: Request) -> dict[str, Any]:
    """The one thing this dashboard's whole login exists to protect.

    Reads `request.app.state.report` (set once at startup in `app.py`'s
    `lifespan`) rather than calling `run_preflight()` per request --
    `check_sandbox_workspace_sharing` inside it launches a real Docker probe
    container, so re-running it on every dashboard poll would spawn one every
    few seconds. `GET /health` avoids this the same way; follow that
    precedent here too.
    """
    report = getattr(request.app.state, "report", None) or run_preflight()
    async with session_scope() as session:
        snapshot = await dashboard_metrics.collect(session, settings=get_settings(), report=report)
    return snapshot.as_dict()


class InterruptReplySubmission(BaseModel):
    response: str = Field(min_length=1, max_length=4000)


@router.post(
    "/interrupts/{interrupt_id}/resolve",
    dependencies=[Depends(require_bearer_token("dashboard_auth_token"))],
)
async def resolve_interrupt(interrupt_id: str, body: InterruptReplySubmission) -> dict[str, str]:
    """Answers an open interrupt exactly the way a Telegram reply would --
    same dispatch, same per-reason actions (`hitl/interrupts.py::resolve_reply`).
    404s on an unknown or already-resolved id rather than silently no-op'ing,
    so a stale dashboard tab gets a clear error instead of an unexplained
    non-effect."""
    async with session_scope() as session:
        target = await session.get(Interrupt, interrupt_id)
        if target is None or target.status != "open":
            raise HTTPException(status_code=404, detail="no open interrupt with that id")
        action = await hitl_interrupts.resolve_reply(session, target, body.response.strip())
    return {"status": "resolved", "action": action}


@router.get(
    "/integrations", dependencies=[Depends(require_bearer_token("dashboard_auth_token"))]
)
async def integrations_state() -> dict[str, Any]:
    """The credential catalog (configured/not, masked previews only -- never a
    real secret value) plus open usage-limit/quota alerts, deliberately kept
    off `GET /dashboard/state`'s own payload: this page is checked far less
    often than the main dashboard poll, and a credential catalog has no
    reason to round-trip on every 5s tick alongside it."""
    async with session_scope() as session:
        snapshot = await integrations_metrics.collect(session, settings=get_settings())
    return snapshot.as_dict()


class CredentialWriteSubmission(BaseModel):
    value: str = Field(max_length=4000)


@router.post(
    "/integrations/credentials/{key}",
    dependencies=[Depends(require_bearer_token("dashboard_auth_token"))],
)
async def write_credential(key: str, body: CredentialWriteSubmission) -> dict[str, Any]:
    """Persists a new value to the real `.env` file backing this service's
    `env_file: .env` (see `settings.py::env_file_path`). Deliberately narrow:
    only a key already in `integrations.CREDENTIAL_CATALOG` and marked
    editable may be written -- an arbitrary key would let this endpoint set
    unrelated process configuration, not just a known credential.

    Never takes effect on the running process -- `Settings` is a cached
    singleton read once at boot -- so the response is explicit that a
    `docker compose up -d factory` is still required, rather than implying
    the change is already live.
    """
    spec = integrations_metrics.catalog_field(key)
    if spec is None or not spec.editable:
        raise HTTPException(status_code=404, detail="no editable credential with that key")

    settings = get_settings()
    if settings.env_file_path is None:
        raise HTTPException(
            status_code=503, detail="ENV_FILE_PATH is not configured -- cannot write credentials"
        )

    try:
        write_env_value(settings.env_file_path, key.upper(), body.value.strip())
    except InvalidEnvValue as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"status": "saved", "restart_required": True}


class CustomCredentialCreate(BaseModel):
    key: str = Field(min_length=2, max_length=64)
    value: str = Field(max_length=4000)


@router.post(
    "/integrations/credentials",
    dependencies=[Depends(require_bearer_token("dashboard_auth_token"))],
)
async def create_custom_credential(body: CustomCredentialCreate) -> dict[str, Any]:
    """The free-form counterpart to `write_credential` above -- for a
    credential this factory doesn't have a catalog entry (or adapter code)
    for yet. Same underlying write (`write_env_value`); the only difference
    is the key comes from the request body instead of the URL, and it's
    registered in `custom_credentials` so the Integrations page can list and
    preview it back (an arbitrary key isn't a typed `Settings` field
    `getattr` can read).
    """
    key = body.key.strip().upper()
    error = integrations_metrics.validate_custom_key(key)
    if error is not None:
        raise HTTPException(status_code=400, detail=error)

    settings = get_settings()
    if settings.env_file_path is None:
        raise HTTPException(
            status_code=503, detail="ENV_FILE_PATH is not configured -- cannot write credentials"
        )

    try:
        write_env_value(settings.env_file_path, key, body.value.strip())
    except InvalidEnvValue as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with session_scope() as session:
        await repository.create_custom_credential(session, key)

    return {"status": "saved", "restart_required": True}


@router.post(
    "/integrations/alerts/{alert_id}/resolve",
    dependencies=[Depends(require_bearer_token("dashboard_auth_token"))],
)
async def resolve_integration_alert(alert_id: str) -> dict[str, str]:
    async with session_scope() as session:
        resolved = await repository.resolve_integration_alert(session, alert_id)
    if not resolved:
        raise HTTPException(status_code=404, detail="no open integration alert with that id")
    return {"status": "resolved"}


@router.get(
    "/products/{product_id}", dependencies=[Depends(require_bearer_token("dashboard_auth_token"))]
)
async def product_detail(product_id: str) -> dict[str, Any]:
    """Fetched on demand when an operator opens one product's card --
    deliberately not part of `GET /dashboard/state`'s bulk snapshot, which
    only needs the lightweight summary row already there."""
    async with session_scope() as session:
        detail = await dashboard_metrics.collect_product_detail(session, product_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="no product with that id")
    return detail.as_dict()


@router.post(
    "/products/{product_id}/design-brief/generate",
    dependencies=[Depends(require_bearer_token("dashboard_auth_token"))],
)
async def generate_design_brief(product_id: str) -> dict[str, str]:
    """Writes intent only -- this endpoint never calls an LLM itself (see
    `development/design_brief.py`'s own module docstring for why: the HTTP
    surface is an observation/ingest layer, not a control plane). The tick
    loop's next pass (`development/graph.py`'s `GENERATE_DESIGN_BRIEF`
    branch) does the real work, within one tick (<=15s). Idempotent: a
    repeat click while one is already queued or already cached is a free
    no-op (`enqueue_task`'s own idempotency key, `build_design_brief`'s own
    cache check).
    """
    async with session_scope() as session:
        product = await session.get(Product, product_id)
        if product is None:
            raise HTTPException(status_code=404, detail="no product with that id")
        if not product.deploy_url:
            raise HTTPException(status_code=400, detail="product has no live deploy_url yet")
        await repository.enqueue_task(
            session,
            kind=TaskKind.GENERATE_DESIGN_BRIEF,
            engine=EngineName.DEVELOPMENT,
            idempotency_key=f"design-brief:{product_id}",
            payload={"product_id": product_id},
            product_id=product_id,
            priority=200,
        )
    return {"status": "queued"}
