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
from factory.hitl import interrupts as hitl_interrupts
from factory.observability import dashboard as dashboard_metrics
from factory.preflight import run_preflight
from factory.settings import get_settings
from factory.state.db import session_scope
from factory.state.orm import Interrupt

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
