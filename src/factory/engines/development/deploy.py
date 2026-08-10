"""Deployment.

Reached only after the Bug Council approves unanimously. Nothing here is permitted to
run before that gate — the ordering is enforced by the caller in ``graph.py`` and
asserted by ``deploy_requires_approval``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import httpx

from factory.logging import get_logger
from factory.sandbox.runner import resolve_workspace
from factory.settings import Settings
from factory.state.schemas import CouncilResult

log = get_logger(__name__)

_VERCEL_ENDPOINT = "https://api.vercel.com/v13/deployments"
_TIMEOUT = httpx.Timeout(90.0, connect=10.0)
_MAX_FILE_BYTES = 2_000_000


class DeployNotPermitted(RuntimeError):
    """An attempt to deploy code the Council did not approve."""


@dataclass(slots=True)
class DeployResult:
    ok: bool
    url: str | None
    detail: str


def deploy_requires_approval(council: CouncilResult) -> None:
    if not council.approved:
        raise DeployNotPermitted(
            "the Bug Council did not approve this build; deployment is not permitted"
        )


def _collect_files(workspace: str, settings: Settings) -> list[dict[str, str]]:
    root = resolve_workspace(workspace, settings)
    # .deps: where _verify_and_ship's install step puts dependencies for the sandbox
    # test run (see development/graph.py) -- Vercel installs from requirements.txt
    # itself at deploy time, so shipping this tree too is both pointless and, for
    # anything non-trivial, likely to blow the file-count/size limits of a
    # no-linked-repo file-upload deployment.
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".deps"}
    payload: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in skip for part in path.parts):
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
            ok=False, url=None, detail="VERCEL_TOKEN is not configured; cannot publish."
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
        return DeployResult(ok=False, url=None, detail=f"deploy transport error: {exc}")

    if response.status_code not in (200, 201):
        return DeployResult(
            ok=False,
            url=None,
            detail=f"vercel returned {response.status_code}: {response.text[:300]}",
        )

    payload = response.json()
    url = payload.get("url")
    full_url = f"https://{url}" if url else None
    log.info("deploy.published", product=product_name, url=full_url)
    return DeployResult(ok=True, url=full_url, detail="published")
