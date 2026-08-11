"""Built-in tools.

Importing this module populates ``REGISTRY``. Each tool declares the credential or
sandbox it needs so the registry can withhold it when that precondition is unmet.
"""

from __future__ import annotations

from typing import Any

import httpx

from factory.logging import get_logger
from factory.settings import get_settings
from factory.tools.registry import REGISTRY

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


@REGISTRY.tool(
    name="http_get",
    description=(
        "Fetch a public URL and return its status code and body text (truncated to "
        "20000 characters). Use for reading documentation, public issue trackers, and "
        "market-signal pages. Does not send credentials."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL to fetch."},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    category="http",
)
async def http_get(url: str) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        return {"error": "url must be absolute http(s)"}
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "superagent-factory/0.1"})
    return {
        "status_code": response.status_code,
        "url": str(response.url),
        "body": response.text[:20_000],
    }


@REGISTRY.tool(
    name="run_in_sandbox",
    description=(
        "Execute a shell command inside an ephemeral, network-isolated Docker "
        "container scoped to one product workspace. Returns exit code, stdout and "
        "stderr. The only sanctioned way to run generated code."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to run."},
            "workspace": {"type": "string", "description": "Product workspace directory name."},
        },
        "required": ["command", "workspace"],
        "additionalProperties": False,
    },
    category="sandbox",
    requires_sandbox=True,
)
async def run_in_sandbox(command: str, workspace: str) -> dict[str, Any]:
    from factory.sandbox.runner import get_sandbox
    from factory.settings import EngineName
    from factory.state import repository
    from factory.state.db import session_scope
    from factory.supervisor.concurrency import LockUnavailable, product_lock

    settings = get_settings()
    sandbox = get_sandbox(settings)

    # This tool handler receives only the arguments the model supplied (matching
    # its input_schema) -- no session/ctx is threaded through the agent runtime's
    # tool-calling convention, so unlike development/graph.py's own writers
    # (_build/_patch, which hold product_lock for their whole clear+write+verify
    # cycle), an ephemeral agent given sandbox access (only RoleName.REPAIR_ENGINEER,
    # spawned e.g. by the "improve-test-coverage" maintenance item) had no
    # contention protection at all: it could run shell commands against a
    # product's workspace at the exact moment Development was mid-clear_workspace/
    # write_plan for that same product. Opens its own short-lived session (the
    # same process-wide cached engine every other session-less call site uses)
    # purely to resolve and hold the lock for the duration of this one command.
    async with session_scope(settings) as session:
        product = await repository.get_product_by_workspace(session, workspace)
        if product is None:
            # Not every workspace belongs to a tracked product (ad hoc/test
            # workspaces) -- nothing to protect against contention over.
            result = await sandbox.execute(command, workspace=workspace)
        else:
            try:
                async with product_lock(
                    session,
                    product_id=product.id,
                    holder=EngineName.SUPERVISOR,
                    reason="ephemeral agent running a sandbox command",
                ):
                    result = await sandbox.execute(command, workspace=workspace)
            except LockUnavailable:
                return {
                    "exit_code": -1,
                    "stdout": "",
                    "stderr": (
                        f"workspace {workspace!r} is currently locked by another "
                        "in-progress build/patch -- try again once it finishes."
                    ),
                    "timed_out": False,
                }

    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout[:20_000],
        "stderr": result.stderr[:20_000],
        "timed_out": result.timed_out,
    }


_LEAD_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "job_titles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Exact job titles a contact database would hold.",
        },
        "seniorities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "junior | senior | manager | director | vp | executive | owner",
        },
        "departments": {"type": "array", "items": {"type": "string"}},
        "company_domains": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Explicit company domains. Never invent these.",
        },
        "company_keywords": {"type": "array", "items": {"type": "string"}},
        "locations": {"type": "array", "items": {"type": "string"}},
        "limit": {"type": "integer", "description": "Maximum candidates to return (1-50)."},
    },
    "required": ["job_titles"],
    "additionalProperties": False,
}

_LEAD_TOOL_DESCRIPTION = (
    "Search {provider} for business contacts matching a role profile. Returns only "
    "first name, last name, job title, company, work email and provenance — personal "
    "emails, phone numbers and location data are discarded at the API boundary and "
    "are never returned. Every result must still pass the compliance rules and a "
    "Legitimate Interest Assessment before it can be stored or contacted. Call this "
    "when you need prospects for a live product and know which roles would buy it."
)


async def _run_lead_search(provider_name: str, **kwargs: Any) -> dict[str, Any]:
    from factory.engines.marketing.providers import (
        ApolloProvider,
        HunterProvider,
        LeadSearchPlan,
    )

    settings = get_settings()
    limit = int(kwargs.pop("limit", 25) or 25)
    plan = LeadSearchPlan(rationale="requested via tool call", **kwargs)
    provider = HunterProvider(settings) if provider_name == "hunter" else ApolloProvider(settings)
    if not provider.available:
        return {"error": f"{provider_name} is not configured", "candidates": []}

    candidates = await provider.search(plan, limit=max(1, min(limit, 50)))
    return {
        "provider": provider.name,
        "count": len(candidates),
        # Deliberately the narrowed shape: a tool result becomes model context, so
        # returning the raw provider payload here would put personal data the factory
        # refuses to store into a prompt instead.
        "candidates": [
            {
                "job_title": c.job_title,
                "company": c.company,
                "email_domain": c.work_email.partition("@")[2],
                "provenance_kind": str(c.provenance.kind),
                "source_url": c.provenance.url,
            }
            for c in candidates
        ],
    }


@REGISTRY.tool(
    name="find_leads_hunter",
    description=_LEAD_TOOL_DESCRIPTION.format(provider="Hunter.io"),
    input_schema=_LEAD_SEARCH_SCHEMA,
    category="leads",
    required_credential="HUNTER_API_KEY",
)
async def find_leads_hunter(**kwargs: Any) -> dict[str, Any]:
    return await _run_lead_search("hunter", **kwargs)


@REGISTRY.tool(
    name="find_leads_apollo",
    description=_LEAD_TOOL_DESCRIPTION.format(provider="Apollo.io"),
    input_schema=_LEAD_SEARCH_SCHEMA,
    category="leads",
    required_credential="APOLLO_API_KEY",
)
async def find_leads_apollo(**kwargs: Any) -> dict[str, Any]:
    return await _run_lead_search("apollo", **kwargs)


@REGISTRY.tool(
    name="github_search_issues",
    description=(
        "Search public GitHub issues for recurring complaints and unmet needs. "
        "Returns issue titles, bodies and URLs for market-signal analysis."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "GitHub issue search query."},
            "limit": {"type": "integer", "description": "Maximum results (1-50)."},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    category="git",
    required_credential="GITHUB_TOKEN",
)
async def github_search_issues(query: str, limit: int = 20) -> dict[str, Any]:
    settings = get_settings()
    token = settings.secret("GITHUB_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "superagent-factory/0.1",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {"q": f"{query} is:issue", "per_page": str(max(1, min(limit, 50)))}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.get(
            "https://api.github.com/search/issues", params=params, headers=headers
        )
    if response.status_code != 200:
        return {"error": f"github returned {response.status_code}", "items": []}
    payload = response.json()
    return {
        "total": payload.get("total_count", 0),
        "items": [
            {
                "title": item.get("title", ""),
                "body": (item.get("body") or "")[:2000],
                "url": item.get("html_url", ""),
                "comments": item.get("comments", 0),
            }
            for item in payload.get("items", [])
        ],
    }
