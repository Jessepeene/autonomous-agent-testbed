"""GET /dashboard/state -- the read-only API behind the operator dashboard.

Same posture as GET /metrics (api/app.py): bearer-token gated, an observation
surface, never a control plane. A separate router module rather than another
route straight in app.py because this endpoint has real internal structure
(see observability/dashboard.py) that would otherwise crowd that file's
deliberately small surface.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from factory.api.auth import require_bearer_token
from factory.observability import dashboard as dashboard_metrics
from factory.preflight import run_preflight
from factory.settings import get_settings
from factory.state.db import session_scope

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
