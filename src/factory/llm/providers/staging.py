"""A deterministic stand-in for Anthropic, used only when
``settings.staging_mock_expensive_calls`` is set.

``CODE_GENERATION`` and ``CODE_REVIEW`` are 100% Anthropic-served with no failover
(``llm/router.py::NO_FAILOVER``), and together they are the overwhelming majority of
real dollar cost — MVP scaffolds, landing pages, and Bug Council review all run on
Opus. That makes them the wrong thing to pay for repeatedly while validating the rest
of the pipeline (Discovery, Compliance, lead sourcing, outreach copy — all
Gemini-served and meaningfully cheaper).

This is not ``FakeProvider``: that class is built for tests that register one
response and consume it once. A staging run needs to serve an unbounded number of
calls, for whatever idea Discovery actually produced, and return something that
*actually works* — the point is to exercise the real sandbox install/test cycle, the
real deploy call, and everything downstream against a genuinely functioning product,
not a placeholder that would make every later stage meaningless.

Every quantity in the returned files is real and self-consistent: the MVP fixture's
auth, billing wiring, and test suite genuinely function against the sandbox's actual
Python 3.13 environment. The interest-capture and analytics-beacon URLs are
product-specific — extracted from the prompt text the same deterministic way
`generate_scaffold`/`generate_landing_page` composed them in, never guessed.
"""

from __future__ import annotations

import re
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from factory.engines.development.bug_council import REVIEWER_PROMPTS
from factory.llm.providers.base import (
    AgentTurn,
    Completion,
    Image,
    Provider,
    Turn,
    Usage,
)
from factory.settings import TaskType
from factory.state.schemas import GeneratedFile, ReviewCategory, ReviewVerdict, ScaffoldPlan

SchemaT = TypeVar("SchemaT", bound=BaseModel)

#: `generate_scaffold`/`generate_landing_page` compose these verbatim into their
#: prompts and instruct the model to use them exactly -- deterministic code on the
#: write side, so extracting deterministically here is safe and never guesses at a
#: URL shape. Each label's own worked example (e.g. `{"email": ...}`) contains
#: colons of its own, so anchoring on the first `:` after the label would stop
#: inside the example rather than at the real "verbatim):" marker -- non-greedy
#: `.*?` up to that literal marker (never crossing a newline, since each
#: label+URL is composed as one line) is what correctly skips over it, and what
#: lets the analytics pattern find its own URL rather than the interest one when
#: both are present in the same landing-page prompt.
_INTEREST_URL_RE = re.compile(r"Interest capture URL.*?verbatim\):\s*(\S+)")
_ANALYTICS_URL_RE = re.compile(r"Analytics beacon URL.*?verbatim\):\s*(\S+)")

#: Present only when `generate_landing_page` was called with `ab_variants` --
#: extracted the same deterministic way as the URLs above, anchored on the exact,
#: code-controlled markers `_AB_TEST_INSTRUCTIONS` composes (`generator.py`).
_AB_VARIANTS_RE = re.compile(
    r"Variant A brief:\s*(.*?)\s*\n\nVariant B brief:\s*(.*?)\s*\n\nAdd a small inline script",
    re.DOTALL,
)


def _extract_url(pattern: re.Pattern[str], prompt: str, fallback: str) -> str:
    match = pattern.search(prompt)
    return match.group(1) if match else fallback


def _seo_files() -> list[GeneratedFile]:
    """The two standalone SEO files both prompts require -- shared since their
    content has nothing product-specific about it (see seo.py's deterministic
    check for what these satisfy)."""
    sitemap = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url><loc>https://example.test/</loc></url>\n"
        "</urlset>\n"
    )
    robots = "User-agent: *\nAllow: /\n"
    return [
        GeneratedFile(path="app/sitemap.xml", contents=sitemap),
        GeneratedFile(path="app/robots.txt", contents=robots),
    ]


def _analytics_beacon_script(analytics_url: str, variant_field: str = "") -> str:
    """The exact fire-and-forget pattern both prompts require: never block
    rendering on it, never surface a failure to the visitor. ``variant_field``
    is a literal `, variant: ...` fragment, appended only when an AB test is
    active -- see `_variant_split_script`."""
    return f"""<script>
fetch('{analytics_url}', {{
  method: 'POST',
  headers: {{'Content-Type': 'application/json'}},
  body: JSON.stringify({{
    path: window.location.pathname,
    referrer: document.referrer || null{variant_field}
  }}),
}}).catch(() => {{}});
</script>"""


#: `REVIEWER_PROMPTS` values are fixed, code-controlled text (Bug Council prompts are
#: permanently excluded from the Learning Loop -- `learning/allowlist.py`), so a
#: reverse lookup from the `system` text a call carried is a reliable way to answer
#: "which category is this reviewer call for" without threading a new parameter
#: through `LLMProvider`'s shared protocol.
_CATEGORY_BY_PROMPT: dict[str, ReviewCategory] = {v: k for k, v in REVIEWER_PROMPTS.items()}

_ZERO_USAGE = Usage(input_tokens=0, output_tokens=0, latency_ms=1)


def _variant_split_script() -> str:
    """The exact client-side split pattern `_AB_TEST_INSTRUCTIONS` asks for:
    seeded once via localStorage, then reused so a returning visitor always sees
    the arm they saw before. Deliberately reads `window.__abVariant` rather than
    hardcoding "variant-a"/"variant-b" element visibility inline, so the same
    small script also supplies the variant value the interest-form and analytics
    handlers below tag their POST bodies with.
    """
    return """<script>
window.__abVariant = localStorage.getItem('ab_variant');
if (!window.__abVariant) {
  window.__abVariant = Math.random() < 0.5 ? 'a' : 'b';
  localStorage.setItem('ab_variant', window.__abVariant);
}
document.getElementById('variant-a').hidden = window.__abVariant !== 'a';
document.getElementById('variant-b').hidden = window.__abVariant !== 'b';
</script>"""


def _landing_page_files(prompt: str) -> list[GeneratedFile]:
    interest_url = _extract_url(_INTEREST_URL_RE, prompt, "https://example.test/interest/unknown")
    analytics_url = _extract_url(
        _ANALYTICS_URL_RE, prompt, "https://example.test/analytics/unknown"
    )
    ab_match = _AB_VARIANTS_RE.search(prompt)

    if ab_match is None:
        body_content = """<main>
<h1>A staging fixture, not a real pitch</h1>
<p>This page exists to exercise the pipeline under staging_mock_expensive_calls.</p>
<form id="interest-form">
  <input type="email" name="email" required>
  <textarea name="message"></textarea>
  <button type="submit">Notify me</button>
</form>
<p id="thank-you" hidden>Thanks, we will be in touch.</p>
<p id="error" hidden>Something went wrong. Please try again.</p>
</main>"""
        split_script = ""
        variant_field = ""
    else:
        variant_a_brief, variant_b_brief = ab_match.group(1), ab_match.group(2)
        body_content = f"""<div id="variant-a" hidden>
<main><h1>{variant_a_brief}</h1></main>
</div>
<div id="variant-b" hidden>
<main><h1>{variant_b_brief}</h1></main>
</div>
<form id="interest-form">
  <input type="email" name="email" required>
  <textarea name="message"></textarea>
  <button type="submit">Notify me</button>
</form>
<p id="thank-you" hidden>Thanks, we will be in touch.</p>
<p id="error" hidden>Something went wrong. Please try again.</p>"""
        split_script = _variant_split_script()
        variant_field = ", variant: window.__abVariant"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Staging fixture landing page</title>
<meta name="description" content="A deterministic staging fixture, not a real pitch.">
<meta property="og:title" content="Staging fixture landing page">
<meta property="og:description" content="A deterministic staging fixture, not a real pitch.">
<link rel="canonical" href="https://example.test/">
</head>
<body>
{split_script}
{body_content}
<script>
document.getElementById('interest-form').addEventListener('submit', async (event) => {{
  event.preventDefault();
  const form = event.target;
  try {{
    const response = await fetch('{interest_url}', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{email: form.email.value, message: form.message.value{variant_field}}}),
    }});
    if (!response.ok) {{
      throw new Error('request failed');
    }}
    form.hidden = true;
    document.getElementById('thank-you').hidden = false;
  }} catch (err) {{
    document.getElementById('error').hidden = false;
  }}
}});
</script>
{_analytics_beacon_script(analytics_url, variant_field)}
</body>
</html>
"""
    server = '''"""Staging fixture: serves the static landing page."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

app = FastAPI()
_HERE = Path(__file__).parent


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_HERE / "index.html")
'''
    test = '''"""Staging fixture tests."""

from fastapi.testclient import TestClient

from app.server import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200


def test_page_loads_and_contains_pitch_and_form() -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "interest-form" in body
    assert "<form" in body
'''
    # pytest itself, not just what the tests import: the sandbox installs nothing
    # beyond this file (`development/graph.py`'s install step), and the real
    # e2e test's sandbox is a scripted always-passing fake that never actually
    # runs pip/pytest -- so a fixture that forgot to list its own test runner
    # passed every check until the first real live build against a real,
    # bare sandbox actually tried to run `pytest tests/ -q` and found it missing.
    requirements = "fastapi>=0.115,<1\nuvicorn>=0.30,<1\nhttpx>=0.27,<1\npytest>=8.0,<9\n"
    readme = "# Staging fixture\n\nRun: `uvicorn app.server:app`\nTest: `pytest tests/ -q`\n"
    return [
        GeneratedFile(path="app/__init__.py", contents=""),
        GeneratedFile(path="app/index.html", contents=html),
        GeneratedFile(path="app/server.py", contents=server),
        GeneratedFile(path="tests/__init__.py", contents=""),
        GeneratedFile(path="tests/test_page.py", contents=test),
        GeneratedFile(path="requirements.txt", contents=requirements),
        GeneratedFile(path="README.md", contents=readme),
        *_seo_files(),
    ]


def _mvp_files(prompt: str) -> list[GeneratedFile]:
    analytics_url = _extract_url(
        _ANALYTICS_URL_RE, prompt, "https://example.test/analytics/unknown"
    )
    server = '''"""Staging fixture: a minimal but genuinely functional auth + billing MVP."""

import hashlib
import hmac
import os
import re
import secrets
from pathlib import Path

import stripe
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

app = FastAPI()
_HERE = Path(__file__).parent
_security = HTTPBearer(auto_error=False)
_EMAIL_RE = re.compile(r"^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$")

# In-memory store: a staging fixture, not a real product's persistence layer.
_users: dict[str, str] = {}
_tokens: dict[str, str] = {}


def _hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    salt, _, digest_hex = stored.partition("$")
    expected = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return hmac.compare_digest(expected.hex(), digest_hex)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=200)

    @field_validator("email")
    @classmethod
    def _valid_email(cls, value: str) -> str:
        if not _EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value


class TokenResponse(BaseModel):
    token: str


class CheckoutResponse(BaseModel):
    checkout_url: str


def _current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_security),
) -> str:
    if credentials is None or credentials.credentials not in _tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing token"
        )
    return _tokens[credentials.credentials]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_HERE / "index.html")


@app.post("/auth/signup", response_model=TokenResponse, status_code=201)
def signup(payload: Credentials) -> TokenResponse:
    if payload.email in _users:
        raise HTTPException(status_code=409, detail="account already exists")
    _users[payload.email] = _hash_password(payload.password)
    token = secrets.token_urlsafe(32)
    _tokens[token] = payload.email
    return TokenResponse(token=token)


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: Credentials) -> TokenResponse:
    stored = _users.get(payload.email)
    if stored is None or not _verify_password(payload.password, stored):
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = secrets.token_urlsafe(32)
    _tokens[token] = payload.email
    return TokenResponse(token=token)


@app.post("/billing/checkout", response_model=CheckoutResponse)
def create_checkout(user: str = Depends(_current_user)) -> CheckoutResponse:
    stripe.api_key = os.environ.get("STRIPE_API_KEY", "")
    product_id = os.environ.get("PRODUCT_ID", "")
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": 4900,
                    "recurring": {"interval": "month"},
                    "product_data": {"name": "Subscription"},
                },
                "quantity": 1,
            }
        ],
        success_url="https://example.test/success",
        cancel_url="https://example.test/cancel",
        client_reference_id=product_id,
    )
    return CheckoutResponse(checkout_url=session.url)
'''
    index_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Staging fixture MVP</title>
<meta name="description" content="A deterministic staging fixture exercising the pipeline.">
<meta property="og:title" content="Staging fixture MVP">
<meta property="og:description" content="A deterministic staging fixture exercising the pipeline.">
<link rel="canonical" href="https://example.test/">
</head>
<body>
<main><h1>Staging fixture</h1><p>Exercises the real build/deploy pipeline.</p></main>
{_analytics_beacon_script(analytics_url)}
</body>
</html>
"""
    test = '''"""Staging fixture tests: core feature + auth boundary."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.server import app

client = TestClient(app)


def test_health() -> None:
    assert client.get("/health").status_code == 200


def test_signup_then_login() -> None:
    email = "user@example.test"
    signup = client.post("/auth/signup", json={"email": email, "password": "correcthorse"})
    assert signup.status_code == 201
    token = signup.json()["token"]
    assert token

    login = client.post("/auth/login", json={"email": email, "password": "correcthorse"})
    assert login.status_code == 200


def test_signup_rejects_a_short_password_with_422() -> None:
    response = client.post(
        "/auth/signup", json={"email": "short@example.test", "password": "short"}
    )
    assert response.status_code == 422


def test_duplicate_signup_is_rejected() -> None:
    email = "dup@example.test"
    client.post("/auth/signup", json={"email": email, "password": "correcthorse"})
    second = client.post("/auth/signup", json={"email": email, "password": "correcthorse"})
    assert second.status_code == 409


def test_login_with_wrong_password_is_rejected() -> None:
    email = "wrongpw@example.test"
    client.post("/auth/signup", json={"email": email, "password": "correcthorse"})
    response = client.post("/auth/login", json={"email": email, "password": "not-it-at-all"})
    assert response.status_code == 401


def test_checkout_requires_a_token() -> None:
    response = client.post("/billing/checkout")
    assert response.status_code == 401


def test_checkout_creates_a_session_for_an_authenticated_user() -> None:
    email = "billing@example.test"
    signup = client.post("/auth/signup", json={"email": email, "password": "correcthorse"})
    token = signup.json()["token"]

    fake_session = type("FakeSession", (), {"url": "https://checkout.stripe.test/fake"})()
    with patch("app.server.stripe.checkout.Session.create", return_value=fake_session):
        response = client.post(
            "/billing/checkout", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    assert response.json()["checkout_url"] == "https://checkout.stripe.test/fake"
'''
    requirements = (
        "fastapi>=0.115,<1\nuvicorn>=0.30,<1\nhttpx>=0.27,<1\nstripe>=11.0,<12\npytest>=8.0,<9\n"
    )
    readme = "# Staging fixture MVP\n\nRun: `uvicorn app.server:app`\nTest: `pytest tests/ -q`\n"
    return [
        GeneratedFile(path="app/__init__.py", contents=""),
        GeneratedFile(path="app/index.html", contents=index_html),
        GeneratedFile(path="app/server.py", contents=server),
        GeneratedFile(path="tests/__init__.py", contents=""),
        GeneratedFile(path="tests/test_app.py", contents=test),
        GeneratedFile(path="requirements.txt", contents=requirements),
        GeneratedFile(path="README.md", contents=readme),
        *_seo_files(),
    ]


class StagingCodeProvider:
    """Swapped in for Anthropic when ``staging_mock_expensive_calls`` is set."""

    name = Provider.ANTHROPIC

    @property
    def available(self) -> bool:
        return True

    async def parse(
        self,
        schema: type[SchemaT],
        *,
        system: str,
        prompt: str,
        task_type: TaskType,
        model: str,
        max_tokens: int = 8192,
        tools: list[dict[str, Any]] | None = None,
        cache_prefix: str | None = None,
        # Accepted and ignored. A provider without vision answering from the text
        # alone is the right degradation -- failing the call would turn "this model
        # cannot see" into an outage. Callers that genuinely need eyes check
        # `vision_capable` rather than assuming.
        images: list[Image] | None = None,
    ) -> Completion[SchemaT]:
        if schema is ScaffoldPlan:
            # generate_landing_page always includes this exact phrase (see
            # generator.py's prompt composition); the real MVP prompt never does.
            # A code-controlled marker, not a guess -- robust even if the Learning
            # Loop has mutated LANDING_PAGE_SYSTEM's wording elsewhere.
            is_landing_page = "Interest capture URL" in prompt
            files = _landing_page_files(prompt) if is_landing_page else _mvp_files(prompt)
            plan = ScaffoldPlan(
                product_name="Staging Fixture",
                summary=(
                    "A deterministic staging fixture standing in for a real "
                    "generation call, used only when staging_mock_expensive_calls "
                    "is set, so the rest of the pipeline can be exercised without "
                    "paying for repeated Opus-tier calls."
                ),
                files=files,
                test_command="python -m pytest tests/ -q",
            )
            return Completion(
                value=cast(SchemaT, plan), provider=self.name, model=model, usage=_ZERO_USAGE
            )
        if schema is ReviewVerdict:
            category = _CATEGORY_BY_PROMPT.get(system, ReviewCategory.SECURITY)
            verdict = ReviewVerdict(category=category, approved=True, findings=[])
            return Completion(
                value=cast(SchemaT, verdict), provider=self.name, model=model, usage=_ZERO_USAGE
            )
        raise AssertionError(
            f"StagingCodeProvider has no fixture for {schema.__name__} -- this "
            "task type was not anticipated to reach the mocked Anthropic provider; "
            "extend staging.py rather than silently guessing."
        )

    async def converse(
        self,
        *,
        system: str,
        turns: list[Turn],
        tools: list[dict[str, Any]],
        task_type: TaskType,
        model: str,
        max_tokens: int = 8192,
    ) -> AgentTurn:
        # Reached only if a heal cycle fires against a staging-generated product,
        # which the always-passing fixtures above are designed not to trigger. A
        # safe, non-crashing default: report nothing to fix rather than fabricate a
        # plausible-looking tool call against a real workspace.
        return AgentTurn(
            text="Staging fixture: no repair attempted.",
            tool_calls=[],
            stop_reason="end_turn",
            provider=self.name,
            model=model,
            usage=_ZERO_USAGE,
        )

    async def list_models(self) -> list[str]:
        return ["claude-opus-5"]
