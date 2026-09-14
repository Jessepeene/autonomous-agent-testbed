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
    #: The exact Vercel project name/slug this deploy used, on success -- `graph.py`
    #: persists this on the product so a later custom-domain attachment
    #: (`development/domain.py`) targets the same project without re-deriving the
    #: slug from `product_name`, which can drift across builds. `None` on dry-run
    #: or failure, when no real Vercel project was touched.
    project: str | None = None


_INFRA_STATUS = {401, 403, 429, 500, 502, 503, 504}


def deploy_requires_approval(
    council: CouncilResult | None, *, override_reason: str | None = None
) -> None:
    # An explicit human override on their own product is the one path allowed to
    # bypass this gate -- see graph.py's `deploy_accepting_risk`, reached only from
    # a human's own "defer/accept the risk" reply to a heal_exhausted escalation,
    # never from the normal build/patch pipeline. `council` is deliberately left
    # `None` on that path rather than forged as approved: this function's job is to
    # gate *publishing*, not to keep a truthful historical record of what the
    # Council actually said, but a fabricated `CouncilResult(approved=True)` would
    # corrupt exactly that record everywhere else `council.approved` is read.
    if override_reason is not None:
        return
    if council is None:
        raise DeployNotPermitted(
            "no Bug Council review exists for this build; deployment is not permitted"
        )
    # `deploy_blocking`, not `approved`. A reviewer sets `approved=False` for
    # anything medium or above, so `approved` is "nobody found anything worth
    # mentioning" -- an unbounded bar that no real build clears, because there is
    # always another finding.
    #
    # Observed live, and the reason this changed: a blank-slate landing page met
    # every one of its acceptance criteria (0 gaps, 0 unmet MUSTs), the release
    # judgement weighed three medium/low dependency findings and shipped it with
    # them accepted on the record -- and this line refused, on a boolean that cannot
    # read a requirement and does not know what the product is for.
    #
    # What survives is the part that was always the real rail: nothing critical or
    # high ships, in any category, and nothing medium ships in security, secrets or
    # privilege. Those cannot be waved past by any judgement. See
    # `CouncilResult.deploy_blocking`.
    blocking = council.deploy_blocking
    if blocking:
        raise DeployNotPermitted(
            f"{len(blocking)} finding(s) at a severity that cannot ship: "
            + "; ".join(f"[{f.severity}/{f.category}] {f.summary}" for f in blocking[:5])
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
    if not _has_fastapi_entrypoint(payload):
        payload = _without_python_build_triggers(payload)
    return payload


#: Files that make Vercel's zero-config detection classify a deployment as a Python
#: project. Harmless when there is a real backend -- `requirements.txt` is exactly
#: how Vercel learns what to install -- and fatal when there is not.
_PYTHON_BUILD_TRIGGERS = frozenset({"requirements.txt", "pyproject.toml", "Pipfile", "setup.py"})


def _without_python_build_triggers(files: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop dev-only Python files from a deployment that has no Python entrypoint.

    Live root cause of a validation landing page that passed every gate, was judged
    shippable, and then failed to deploy::

        Error: No python entrypoint found. Set "tool.vercel.entrypoint" in
        pyproject.toml or define an entrypoint in one of: app.py, index.py, ...

    The page is pure static HTML by design (`LANDING_PAGE_SYSTEM` forbids backend
    logic). It carried a 15-byte `requirements.txt` holding `pytest`, for the
    `test_validation_page.py` beside it -- both exist so the build can be tested
    locally, and neither has anything to do with what gets served. Vercel saw
    `requirements.txt`, concluded "Python project", and demanded an entrypoint that
    a landing page will never have.

    `_has_fastapi_entrypoint` already draws exactly the right line: a deployment
    with no recognised entrypoint is static, and for a static deployment these files
    can only mislead the builder. A real MVP keeps all of them -- that is how Vercel
    learns what to install.
    """
    kept = []
    for entry in files:
        name = entry["file"].split("/")[-1]
        if entry["file"] in _PYTHON_BUILD_TRIGGERS or name.startswith("test_"):
            log.info("deploy.static_file_excluded", path=entry["file"])
            continue
        kept.append(entry)
    return kept


#: Vercel's zero-config FastAPI detection looks for a `FastAPI` instance named
#: `app` at one of these filenames, at the project root or one level under one of
#: the directories below (confirmed live and against Vercel's own docs, 2026-08).
_FASTAPI_ENTRYPOINT_NAMES = frozenset(
    {"app.py", "index.py", "server.py", "main.py", "wsgi.py", "asgi.py"}
)
_FASTAPI_ENTRYPOINT_DIRS = frozenset({"src", "app", "api"})


def _has_fastapi_entrypoint(files: list[dict[str, str]]) -> bool:
    """Real, live-confirmed root cause of a product's backend never actually
    running once deployed: `deploy()` used to send `projectSettings.framework:
    None` unconditionally, which explicitly tells Vercel not to detect a
    framework at all -- disabling the exact zero-config FastAPI path that would
    otherwise build `app/main.py` (or any of the other recognised entrypoints)
    into a real Vercel Function. Confirmed live via the Deployments API on the
    first product this factory ever shipped: `"functions": null, "builds": []`
    -- every `/api/*` call the frontend made 404'd against static hosting,
    because no backend was ever built, not because of a bug in the backend code
    itself. `deploy()` serves both real MVP builds (which have one of these
    entrypoints) and validation-only landing pages (pure static, no Python
    backend at all -- see `LANDING_PAGE_SYSTEM`'s own "no backend business
    logic" constraint), so this has to detect rather than assume: declaring
    `framework: "fastapi"` for a page with no `app` instance would make Vercel's
    build fail looking for one that was never generated.
    """
    for entry in files:
        parts = entry["file"].split("/")
        if len(parts) == 1 and parts[0] in _FASTAPI_ENTRYPOINT_NAMES:
            return True
        if (
            len(parts) == 2
            and parts[0] in _FASTAPI_ENTRYPOINT_DIRS
            and parts[1] in _FASTAPI_ENTRYPOINT_NAMES
        ):
            return True
    return False


def _price_cents(price_usd: float) -> int:
    """Rounds rather than truncates -- a bare ``int(price_usd * 100)`` silently
    undercharges by a cent on plenty of real price points (``19.99 * 100`` is
    ``1998.9999999999998`` in IEEE-754 double precision, which truncates to 1998,
    not 1999). ``round()`` on a float with no ``ndigits`` already returns an
    ``int`` in Python, and makes the float-to-cents conversion exact for every
    price this system can produce (``self_serve_price_usd`` is capped at
    100_000.0, nowhere near where round-half-to-even could matter at cent
    precision)."""
    return round(price_usd * 100)


def _runtime_env(
    product_id: str, settings: Settings, *, self_serve_price_usd: float | None = None
) -> dict[str, str]:
    """Every generated product needs to know its own factory-assigned id — it is the
    only way a Stripe event the product creates can ever be attributed back to a
    product row, since the factory itself never creates the Checkout Session.

    ``self_serve_price_usd`` is ``None`` for a validation landing page (no Stripe
    wiring at all, per `LANDING_PAGE_SYSTEM` -- there is nothing for
    PRODUCT_PRICE_CENTS to control) and a real float for every real MVP build, even
    0.0 for an idea scored before this field existed -- 0.0 is a real, if
    degenerate, price, not a missing value the caller needs to special-case.
    """
    env = {"PRODUCT_ID": product_id}
    if self_serve_price_usd is not None:
        env["PRODUCT_PRICE_CENTS"] = str(_price_cents(self_serve_price_usd))
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
    council: CouncilResult | None,
    product_id: str,
    product_name: str,
    workspace: str,
    settings: Settings,
    dry_run: bool,
    self_serve_price_usd: float | None = None,
    override_reason: str | None = None,
) -> DeployResult:
    deploy_requires_approval(council, override_reason=override_reason)
    if override_reason is not None:
        log.warning(
            "deploy.override",
            product_id=product_id,
            reason=override_reason[:300],
        )

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

    # `env` on a no-linked-repo file-upload `POST /v13/deployments` call accepts a
    # flat {"KEY": "value"} object -- confirmed live, the request is accepted
    # without error. Whether the deployed function actually reads these values at
    # runtime depends on `_has_fastapi_entrypoint` above finding a real backend to
    # run them in -- see that function's docstring for why this was never
    # exercised until now.
    project_name = product_name.lower().replace(" ", "-")[:52]
    framework = "fastapi" if _has_fastapi_entrypoint(files) else None
    body = {
        "name": project_name,
        "files": files,
        "target": "production",
        "projectSettings": {"framework": framework},
        "env": _runtime_env(product_id, settings, self_serve_price_usd=self_serve_price_usd),
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                _VERCEL_ENDPOINT,
                params={"skipAutoDetectionConfirmation": "1"},
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
        # The state name alone is not a defect report. This detail becomes the entire
        # brief a repair agent is handed ("Deployment failed: {detail}"), and
        # `readyState=ERROR` tells it nothing it can act on -- it cannot even tell a
        # syntax error from a missing entrypoint from an out-of-memory build. Observed
        # live: a landing page failed on "No python entrypoint found", the agent was
        # told only "readyState=ERROR", and no amount of repair budget could have
        # closed that gap. Vercel publishes the build log; ask for it.
        build_log = await _build_error_text(deployment_id, token) if deployment_id else ""
        return DeployResult(
            ok=False,
            url=full_url,
            detail=(
                f"vercel build did not succeed (readyState={ready_state or 'unknown'})"
                + (f"\n\nVercel build log:\n{build_log}" if build_log else "")
            ),
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

    # A page nobody can open has not shipped. Vercel protects every `*.vercel.app`
    # URL on a new project by default, so this has to run on each deploy -- the
    # project is created by the call above, and there is no earlier point to do it.
    await _disable_sso_protection(project_name, token)
    log.info("deploy.published", product=product_name, url=full_url)
    return DeployResult(ok=True, url=full_url, detail="published", project=project_name)


async def _disable_sso_protection(project_name: str, token: str) -> None:
    """Make the deployment publicly reachable. Best-effort, never blocks a deploy.

    Vercel protects every deployment URL on a new project by default
    (`ssoProtection: {"deploymentType": "all_except_custom_domains"}`), so the
    `*.vercel.app` URL 302s to `vercel.com/sso-api` and any visitor gets a login
    page. A validation landing page exists to measure whether strangers want the
    idea; behind a login it measures nothing, and the lead harvest that runs
    straight after the deploy reads zero from a page nobody could open.

    This was found and fixed by hand on one project on 2026-08-20 (BUG_LOG 5),
    which recorded the residual gap precisely: *"Every future product's Vercel
    project will need the same manual step ... until this is automated in
    `deploy.py` itself."* It then recurred on the next project ever created, which
    is what this is.

    The tradeoff is the one taken deliberately then: this disables protection for
    every deployment on the project, previews included. Each project here hosts
    exactly one product whose entire purpose is to be publicly reachable, so there
    is nothing on it that protection was guarding. A real custom domain is exempt
    by design and remains the better end state -- see `domain.ensure_requested`.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.patch(
                f"https://api.vercel.com/v9/projects/{project_name}",
                json={"ssoProtection": None},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        # The deployment itself succeeded; a page that is up but gated is still a
        # better outcome than failing the deploy over it.
        log.warning("deploy.sso_disable_failed", project=project_name, detail=str(exc)[:200])
        return
    if response.status_code != 200:
        log.warning(
            "deploy.sso_disable_rejected",
            project=project_name,
            status=response.status_code,
            detail=response.text[:200],
        )
        return
    log.info("deploy.sso_disabled", project=project_name)


#: How much build log to carry back. Vercel's failures put the actionable line at
#: the end (the error, then a docs link), so this keeps the tail rather than the head.
_BUILD_LOG_CHARS = 3000


async def _build_error_text(deployment_id: str, token: str) -> str:
    """Vercel's own build output for a failed deployment, or "" if unavailable.

    Best-effort by design: a deploy that already failed must not also fail to report,
    so every error here degrades to the state name the caller already has.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"https://api.vercel.com/v3/deployments/{deployment_id}/events",
                params={"limit": 200},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            log.warning("deploy.build_log_unavailable", status=response.status_code)
            return ""
        body = response.json()
        events = body if isinstance(body, list) else body.get("events", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("deploy.build_log_failed", detail=str(exc)[:200])
        return ""

    lines = []
    for event in events:
        payload = event.get("payload") if isinstance(event, dict) else None
        text = (payload or {}).get("text") or (event.get("text") if isinstance(event, dict) else "")
        if isinstance(text, str) and text.strip():
            lines.append(f"[{event.get('type', 'log')}] {text.strip()}")
    return "\n".join(lines)[-_BUILD_LOG_CHARS:]


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
