"""Deployment.

Reached only after the Bug Council approves unanimously. Nothing here is permitted to
run before that gate — the ordering is enforced by the caller in ``graph.py`` and
asserted by ``deploy_requires_approval``.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass

import httpx

from factory.engines.development.scaffold.generator import WORKSPACE_SKIP_DIRS
from factory.logging import get_logger
from factory.sandbox.runner import resolve_workspace
from factory.settings import Settings
from factory.state.schemas import CouncilResult

log = get_logger(__name__)

_VERCEL_ENDPOINT = "https://api.vercel.com/v13/deployments"
_TIMEOUT = httpx.Timeout(90.0, connect=10.0)
_MAX_FILE_BYTES = 2_000_000
#: Terminal readyState values Vercel uses on both the create-deployment response
#: and the polling endpoint. Anything else (QUEUED, INITIALIZING, BUILDING, ...)
#: means the build has not finished yet.
_READY_STATES = {"READY"}
_FAILED_STATES = {"ERROR", "CANCELED"}
#: Bounded polling for the build to actually finish -- returning ok=True the
#: moment Vercel *accepts* the upload (the previous behaviour) meant a product
#: could go LIVE, and marketing start harvesting leads against it, before the
#: build had even started, let alone succeeded.
_POLL_ATTEMPTS = 6
_POLL_DELAY_S = 10.0


class DeployNotPermitted(RuntimeError):
    """An attempt to deploy code the Council did not approve."""


@dataclass(slots=True)
class DeployResult:
    ok: bool
    url: str | None
    detail: str
    #: A transport failure, missing credential, auth/permission error, rate limit,
    #: or 5xx from Vercel -- none of these are ever the fault of the deployed code,
    #: and none of them can be fixed by regenerating it. `graph.py` raises
    #: `DevOpsError` instead of spending a heal cycle when this is set. A genuine
    #: 4xx *content* rejection (malformed payload, oversized file) is not infra and
    #: leaves this False, since a different generation attempt might avoid it.
    is_infra: bool = False


_INFRA_STATUS = {401, 403, 429, 500, 502, 503, 504}


def deploy_requires_approval(council: CouncilResult) -> None:
    if not council.approved:
        raise DeployNotPermitted(
            "the Bug Council did not approve this build; deployment is not permitted"
        )


def _collect_files(workspace: str, settings: Settings) -> list[dict[str, str]]:
    root = resolve_workspace(workspace, settings)
    # Must exclude exactly what read_workspace() (what Bug Council reviews) excludes
    # -- WORKSPACE_SKIP_DIRS is the single shared list. A previous, separately
    # maintained skip set here omitted dist/build (and .pytest_cache), so a secret
    # or malicious code baked into build output could ship without ever having been
    # seen by any reviewer.
    payload: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in WORKSPACE_SKIP_DIRS for part in path.parts):
            continue
        raw = path.read_bytes()
        if len(raw) > _MAX_FILE_BYTES:
            log.warning("deploy.file_skipped", path=str(path.name), bytes=len(raw))
            continue
        payload.append(
            {
                "file": str(path.relative_to(root)).replace("\\", "/"),
                "data": base64.b64encode(raw).decode("ascii"),
                "encoding": "base64",
            }
        )
    return payload


def _runtime_env(product_id: str, settings: Settings) -> dict[str, str]:
    """Every generated product needs to know its own factory-assigned id — it is the
    only way a Stripe event the product creates can ever be attributed back to a
    product row, since the factory itself never creates the Checkout Session."""
    env = {"PRODUCT_ID": product_id}
    stripe_key = settings.secret("STRIPE_API_KEY")
    if stripe_key:
        env["STRIPE_API_KEY"] = stripe_key
    else:
        # Every generated product's Stripe Checkout wiring reads this key from its
        # own environment (see the scaffold system prompt) -- omitting it silently
        # ships a product that goes LIVE with checkout that can never work.
        log.warning("deploy.stripe_key_missing", product_id=product_id)
    return env


async def deploy(
    *,
    council: CouncilResult,
    product_id: str,
    product_name: str,
    workspace: str,
    settings: Settings,
    dry_run: bool,
) -> DeployResult:
    deploy_requires_approval(council)

    if dry_run:
        log.info("deploy.dry_run", product=product_name, workspace=workspace)
        return DeployResult(
            ok=True,
            url=f"dry-run://{workspace}",
            detail="FACTORY_DRY_RUN is set; the build was approved but not published.",
        )

    token = settings.secret("VERCEL_TOKEN")
    if not token:
        return DeployResult(
            ok=False,
            url=None,
            detail="VERCEL_TOKEN is not configured; cannot publish.",
            is_infra=True,
        )

    files = _collect_files(workspace, settings)
    if not files:
        return DeployResult(ok=False, url=None, detail="workspace contained no deployable files")

    # UNVERIFIED AGAINST A LIVE VERCEL PROJECT: this assumes `env` on a no-linked-repo
    # file-upload `POST /v13/deployments` call accepts a flat {"KEY": "value"} object
    # the same way older deployment-create contracts did. Vercel's actual current
    # contract may instead require a prior `POST /v10/projects/{id}/env` call against
    # an existing project. Confirm against a real sandbox project before relying on
    # this in production — see the plan's Phase 1 spike note.
    body = {
        "name": product_name.lower().replace(" ", "-")[:52],
        "files": files,
        "target": "production",
        "projectSettings": {"framework": None},
        "env": _runtime_env(product_id, settings),
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                _VERCEL_ENDPOINT,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        return DeployResult(
            ok=False, url=None, detail=f"deploy transport error: {exc}", is_infra=True
        )

    if response.status_code not in (200, 201):
        return DeployResult(
            ok=False,
            url=None,
            detail=f"vercel returned {response.status_code}: {response.text[:300]}",
            is_infra=response.status_code in _INFRA_STATUS,
        )

    payload = response.json()
    url = payload.get("url")
    full_url = f"https://{url}" if url else None
    deployment_id = payload.get("id") or payload.get("uid")
    ready_state = str(payload.get("readyState") or "").upper()

    terminal = _READY_STATES | _FAILED_STATES
    if ready_state not in terminal and deployment_id:
        ready_state = await _await_ready(deployment_id, token)

    if ready_state in _FAILED_STATES:
        return DeployResult(
            ok=False,
            url=full_url,
            detail=f"vercel build did not succeed (readyState={ready_state or 'unknown'})",
        )
    if ready_state not in _READY_STATES:
        # Still building after the poll budget -- Vercel taking unusually long is
        # not evidence the generated code is broken.
        return DeployResult(
            ok=False,
            url=full_url,
            detail=f"vercel build did not finish within the poll budget "
            f"(last readyState={ready_state or 'unknown'})",
            is_infra=True,
        )

    log.info("deploy.published", product=product_name, url=full_url)
    return DeployResult(ok=True, url=full_url, detail="published")


async def _await_ready(deployment_id: str, token: str) -> str:
    """Poll until the build reaches a terminal readyState, or the budget runs out.

    Returns the last known readyState (possibly non-terminal, if the budget was
    exhausted) rather than raising -- the caller decides what that means.
    """
    for attempt in range(_POLL_ATTEMPTS):
        await asyncio.sleep(_POLL_DELAY_S)
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.get(
                    f"{_VERCEL_ENDPOINT}/{deployment_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            log.warning("deploy.poll_transport_error", attempt=attempt, detail=str(exc)[:200])
            continue
        if response.status_code != 200:
            log.warning("deploy.poll_failed", attempt=attempt, status=response.status_code)
            continue
        state = str(response.json().get("readyState") or "").upper()
        if state in _READY_STATES or state in _FAILED_STATES:
            return state
    return ""
