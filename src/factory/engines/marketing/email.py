"""Outbound email — the hard blacklist gate.

``send_email()`` parses the root ``blacklist.txt`` on **every** call and returns
``False`` before touching any transport when the recipient matches. This is a file
read in code, not an instruction in a prompt, because a suppression list a model can
reason its way around is not a suppression list.

Three properties this file guarantees:

1. **Fail closed.** If ``blacklist.txt`` is missing or unreadable, nothing is sent.
   A missing suppression list is treated as "everyone is suppressed", never as
   "nobody is".
2. **Checked last.** The blacklist is consulted after every other decision, so no
   caller can reach the transport by taking a different branch.
3. **No model involvement.** Nothing in this module calls an LLM.

Transport is a deliverability API (Mailgun), not raw SMTP: a burst of cold outreach
sent via SMTP straight from the primary domain has no IP rotation, no warmup, and no
bounce/complaint feedback loop, and burns the domain's sender reputation in days. See
``engines/marketing/deliverability.py`` for what happens with the bounce/complaint
signal Mailgun's webhook sends back.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from factory.logging import get_logger
from factory.settings import Settings, get_settings

log = get_logger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


@dataclass(slots=True)
class SendResult:
    sent: bool
    reason: str

    def __bool__(self) -> bool:
        return self.sent


class BlacklistUnavailable(RuntimeError):
    """The suppression list could not be read, so sending is refused."""


def _parse_rules(text: str) -> list[str]:
    rules: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip().lower()
        if line:
            rules.append(line)
    return rules


def load_rules(path: Path) -> list[str]:
    """Read the suppression rules. Raises if the file cannot be read."""
    try:
        return _parse_rules(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BlacklistUnavailable(f"cannot read blacklist at {path}: {exc}") from exc


def matches_rule(email: str, rule: str) -> bool:
    """Exact address, ``@domain``, or ``*.domain`` (domain plus subdomains)."""
    address = email.strip().lower()
    domain = address.partition("@")[2]
    if not domain:
        return False
    if rule.startswith("*."):
        suffix = rule[2:]
        return domain == suffix or domain.endswith("." + suffix)
    if rule.startswith("@"):
        return domain == rule[1:]
    return address == rule


def blacklist_match(email: str, settings: Settings | None = None) -> str | None:
    """The rule that suppresses this address, or None.

    Reads the file on every call so an address added a second ago is honoured
    immediately — including one appended by a GDPR erasure request.
    """
    settings = settings or get_settings()
    rules = load_rules(settings.blacklist_path)
    for rule in rules:
        if matches_rule(email, rule):
            return rule
    return None


def _build_payload(
    *,
    to: str,
    subject: str,
    body: str,
    from_address: str,
    unsubscribe_url: str | None,
    tracking_id: str | None,
) -> dict[str, Any]:
    """Mailgun's form-encoded message fields. ``h:`` prefixes a raw header; ``v:``
    prefixes a user-defined variable that Mailgun's webhook echoes back verbatim in
    ``event-data.user-variables`` — the only way a later bounce/complaint event can
    be correlated back to this specific send (see ``deliverability.py``).
    """
    payload: dict[str, Any] = {
        "from": from_address,
        "to": to,
        "subject": subject,
        "text": body,
    }
    if unsubscribe_url:
        # RFC 8058: one-click unsubscribe, so opting out never depends on the
        # recipient finding a link in the body.
        payload["h:List-Unsubscribe"] = f"<{unsubscribe_url}>"
        payload["h:List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    if tracking_id:
        payload["v:outreach_log_id"] = tracking_id
    return payload


async def _mailgun_send(payload: dict[str, Any], settings: Settings) -> None:
    """Raises ``httpx.HTTPError`` on any transport failure or non-2xx response; the
    caller decides what to do with that."""
    url = f"{settings.mailgun_base_url.rstrip('/')}/{settings.mailgun_domain}/messages"
    api_key = settings.secret("MAILGUN_API_KEY") or ""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.post(url, auth=("api", api_key), data=payload)
    response.raise_for_status()


async def send_email(
    *,
    to: str,
    subject: str,
    body: str,
    settings: Settings | None = None,
    from_address: str | None = None,
    unsubscribe_url: str | None = None,
    tracking_id: str | None = None,
) -> SendResult:
    """Send one message, or refuse.

    Returns ``SendResult(sent=False, ...)`` — never raises — for every suppression
    path, so a caller that ignores the reason still cannot accidentally send.

    ``tracking_id`` should be the eventual ``OutreachLog.id`` (generated by the
    caller ahead of the row's own insert — see ``outreach.py::send_to_lead``), so
    Mailgun's webhook can report a bounce or complaint against the exact send that
    caused it.

    ``from_address`` overrides ``settings.outbound_from_address`` when given — e.g.
    ``outreach.py::send_to_lead`` composes a display name from the learning loop
    around the fixed mailbox. Callers that don't need per-send identity (e.g.
    transactional confirmation emails) can omit it and get the bare mailbox as
    before.
    """
    settings = settings or get_settings()
    recipient = to.strip().lower()

    # 1. Suppression list. Checked first and fail-closed.
    try:
        rule = blacklist_match(recipient, settings)
    except BlacklistUnavailable as exc:
        log.error("email.blocked_no_blacklist", detail=str(exc))
        return SendResult(False, "blacklist unavailable; refusing to send")

    if rule is not None:
        log.info("email.suppressed", rule=rule, domain=recipient.partition("@")[2])
        return SendResult(False, f"recipient matches blacklist rule {rule!r}")

    # 2. Sender identity is a legal requirement, not a nicety.
    from_address = from_address or settings.outbound_from_address
    if not from_address:
        return SendResult(False, "OUTBOUND_FROM_ADDRESS is not configured")

    # 3. Dry run stops here — after every check, so the checks are exercised.
    if settings.factory_dry_run:
        log.info("email.dry_run", domain=recipient.partition("@")[2], subject=subject[:80])
        return SendResult(False, "FACTORY_DRY_RUN is set; message was composed but not sent")

    if not settings.has_mailgun:
        return SendResult(False, "Mailgun transport is not configured")

    payload = _build_payload(
        to=recipient,
        subject=subject,
        body=body,
        from_address=from_address,
        unsubscribe_url=unsubscribe_url,
        tracking_id=tracking_id,
    )
    try:
        await _mailgun_send(payload, settings)
    except httpx.HTTPError as exc:
        log.warning("email.send_failed", detail=str(exc)[:200])
        return SendResult(False, f"transport error: {exc}")

    log.info("email.sent", domain=recipient.partition("@")[2], subject=subject[:80])
    return SendResult(True, "sent")
