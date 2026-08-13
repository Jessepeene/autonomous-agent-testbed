"""Custom domain request + real Vercel attachment.

A product going LIVE for the first time already works on its Vercel subdomain --
a custom domain is a genuine enhancement, not a blocker, so this is deliberately
NOT a poll-and-defer gate the way `campaign.ensure_approval` is. `ensure_requested`
fires the request once (guarded by the caller checking `domain_status is None`)
and returns immediately; the human's reply (handled in `hitl/interrupts.py`)
enqueues `ATTACH_DOMAIN`, and a periodic sweep (`scheduler.py`) enqueues
`VERIFY_DOMAIN` until Vercel confirms the DNS records are in place.

UNVERIFIED AGAINST A LIVE VERCEL PROJECT, same caveat as `deploy.py`'s own
deployment call: the endpoint versions and response shapes below follow Vercel's
documented "Add a Domain to a Project" / "Get a Project Domain" contract as of
this writing, not a confirmed live call. Confirm before relying on this in
production -- see DEPLOYMENT_CHECKLIST.md's Phase 6 manual runbook entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from factory.hitl import interrupts
from factory.logging import get_logger
from factory.settings import Settings
from factory.state.schemas import InterruptReason

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_INFRA_STATUS = {401, 403, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class DnsRecord:
    type: str
    name: str
    value: str


@dataclass(slots=True)
class AttachResult:
    ok: bool
    verified: bool
    detail: str
    records: list[DnsRecord] = field(default_factory=list)
    #: Same meaning as `deploy.DeployResult.is_infra` -- never the fault of the
    #: generated product, never worth a heal cycle.
    is_infra: bool = False


async def ensure_requested(session: AsyncSession, *, product_id: str, product_name: str) -> None:
    """Ask the human for a domain, once, for a product that just went LIVE.

    The caller (`development/graph.py::_verify_and_ship`) is responsible for only
    calling this when `product.domain_status is None` -- this function does not
    re-check, so calling it twice for the same product would raise two interrupts.
    """
    await interrupts.request(
        session,
        reason=InterruptReason.DOMAIN_REQUIRED,
        subject=f"Domain for {product_name[:100]} ({product_id[:8]})",
        detail=(
            f"{product_name} just went live. This never blocks anything -- the "
            "product is already reachable on its Vercel subdomain either way."
        ),
        headline="\U0001f310 Custom domain?",
        reply_hint=(
            "Reply with a domain you own (e.g. example.com) to attach it, "
            "or SKIP to keep the default Vercel URL."
        ),
        product_id=product_id,
    )


async def attach(settings: Settings, *, project: str, hostname: str) -> AttachResult:
    token = settings.secret("VERCEL_TOKEN")
    if not token:
        return AttachResult(
            ok=False, verified=False, detail="VERCEL_TOKEN is not configured", is_infra=True
        )

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"https://api.vercel.com/v10/projects/{project}/domains",
                json={"name": hostname},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        return AttachResult(
            ok=False, verified=False, detail=f"transport error: {exc}", is_infra=True
        )

    if response.status_code not in (200, 201):
        return AttachResult(
            ok=False,
            verified=False,
            detail=f"vercel returned {response.status_code}: {response.text[:300]}",
            is_infra=response.status_code in _INFRA_STATUS,
        )

    payload = response.json()
    records = [
        DnsRecord(
            type=str(item.get("type", "")),
            name=str(item.get("domain", hostname)),
            value=str(item.get("value", "")),
        )
        for item in payload.get("verification", []) or []
    ]
    return AttachResult(
        ok=True, verified=bool(payload.get("verified")), detail="attached", records=records
    )


async def check_verified(settings: Settings, *, project: str, hostname: str) -> bool | None:
    """Poll whether `hostname` has finished DNS verification.

    Returns `None` on a transport/API failure -- the caller must treat that as
    "try again next sweep," not as "not verified," so a transient Vercel outage
    can never look like a DNS misconfiguration.
    """
    token = settings.secret("VERCEL_TOKEN")
    if not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"https://api.vercel.com/v9/projects/{project}/domains/{hostname}",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        log.warning("domain.verify_transport_error", detail=str(exc)[:200])
        return None
    if response.status_code != 200:
        log.warning("domain.verify_failed", status=response.status_code)
        return None
    return bool(response.json().get("verified"))


def format_dns_instructions(hostname: str, records: list[DnsRecord]) -> str:
    if not records:
        return (
            f"{hostname} has been attached to your Vercel project. No additional DNS "
            "records were returned, so it may already be verified -- the factory will "
            "confirm automatically once it checks."
        )
    lines = [f"To finish attaching {hostname}, add these DNS records at your registrar:"]
    lines.extend(f"  {record.type}  {record.name}  ->  {record.value}" for record in records)
    lines.append("The factory will check back automatically and confirm once verified.")
    return "\n".join(lines)
