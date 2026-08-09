"""Deterministic compliance rules.

These run *before* any model is asked for an opinion, and the model can only narrow
what they allow — never widen it. A rule that a language model could talk itself out
of is not a control.

Jurisdiction: the factory operates from the Netherlands, so GDPR applies to every
prospect regardless of where they are, and CAN-SPAM applies to US recipients.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from factory.state.schemas import LeadRecord

#: Mailbox providers whose addresses are personal rather than professional. B2B
#: legitimate interest is argued from someone's *role*, which a personal mailbox
#: does not evidence — so these are refused outright rather than argued about.
PERSONAL_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "zoho.com",
        "yandex.ru",
        "web.de",
        "t-online.de",
        "ziggo.nl",
        "kpnmail.nl",
        "home.nl",
    }
)

#: Role addresses reach a function, not a person who can be shown a lawful basis.
ROLE_ADDRESS_LOCALPARTS: frozenset[str] = frozenset(
    {
        "info",
        "contact",
        "sales",
        "support",
        "help",
        "admin",
        "webmaster",
        "postmaster",
        "abuse",
        "privacy",
        "legal",
        "noreply",
        "no-reply",
        "donotreply",
    }
)

#: Contacting more than this many people at one company in the window reads as
#: bulk spam rather than targeted outreach, whatever the LIA says.
MAX_CONTACTS_PER_DOMAIN_PER_MONTH = 3

_UNSUBSCRIBE_PATTERN = re.compile(r"unsubscribe|opt[- ]?out|afmelden", re.IGNORECASE)

#: A deterministic backstop behind the prompt-level guidance in
#: `marketing/outreach.py::SYSTEM_PROMPT` — multi-word phrases only, deliberately,
#: since single common words ("free", "cash", "urgent") appear in legitimate B2B
#: copy too often to be a reliable signal on their own. This is a backstop, not the
#: primary defence: a model that ignores its own system prompt is the real problem,
#: and this only catches the phrases blunt enough to enumerate in advance.
_SPAM_TRIGGER_PHRASES: tuple[str, ...] = (
    "act now",
    "buy now",
    "click here",
    "limited time",
    "risk-free",
    "risk free",
    "no obligation",
    "100% free",
    "congratulations",
    "you have been selected",
    "double your income",
    "work from home",
    "cash bonus",
    "not spam",
    "this is not a scam",
    "expires soon",
    "today only",
)


def _spam_trigger_hits(body: str) -> list[str]:
    lowered = body.lower()
    return [phrase for phrase in _SPAM_TRIGGER_PHRASES if phrase in lowered]


@dataclass(slots=True)
class RuleResult:
    passed: bool
    violations: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.passed = False
        self.violations.append(message)


def check_lead_eligibility(
    lead: LeadRecord,
    *,
    contacts_this_month: int = 0,
) -> RuleResult:
    """Hard gates a prospect must clear before an LIA is even worth running."""
    result = RuleResult(passed=True)

    domain = lead.email_domain
    if domain in PERSONAL_EMAIL_DOMAINS:
        result.fail(
            f"{domain} is a personal mailbox provider; B2B legitimate interest cannot "
            "be established for a private address"
        )

    localpart = lead.work_email.partition("@")[0]
    if localpart in ROLE_ADDRESS_LOCALPARTS:
        result.fail(f"{localpart}@ is a role address, not an identifiable data subject")

    if not lead.job_title.strip():
        result.fail("job_title is required to argue a role-to-utility link")

    if contacts_this_month >= MAX_CONTACTS_PER_DOMAIN_PER_MONTH:
        result.fail(
            f"{domain} has already received {contacts_this_month} contacts in the last "
            f"30 days (cap {MAX_CONTACTS_PER_DOMAIN_PER_MONTH})"
        )

    if not lead.source_url:
        result.fail("no source_url: provenance cannot be demonstrated")

    return result


def check_outreach_content(
    body: str,
    *,
    from_address: str | None,
    postal_address: str | None,
    unsubscribe_url: str | None,
) -> RuleResult:
    """CAN-SPAM and GDPR transparency requirements for an outbound message."""
    result = RuleResult(passed=True)

    if not from_address:
        result.fail("no sender identity configured (OUTBOUND_FROM_ADDRESS)")
    if not postal_address:
        result.fail("no physical postal address configured (CAN-SPAM §7704(a)(5))")
    if not unsubscribe_url:
        result.fail("no unsubscribe URL configured")
    elif unsubscribe_url not in body:
        result.fail("message body does not contain the unsubscribe link")

    if not _UNSUBSCRIBE_PATTERN.search(body):
        result.fail("message body does not offer a clear opt-out in words")
    if postal_address and postal_address not in body:
        result.fail("message body does not include the postal address")

    hits = _spam_trigger_hits(body)
    if hits:
        result.fail(f"body contains spam-trigger phrase(s): {', '.join(hits)}")

    return result


def render_compliance_footer(
    *,
    postal_address: str,
    unsubscribe_url: str,
    source_url: str,
    provenance_kind: str = "operator_supplied",
    provider: str = "",
) -> str:
    """The footer every outbound message carries.

    Naming the source is a GDPR Art. 14(2)(f) obligation: the recipient never gave us
    their data, so at first contact they are entitled to know where it came from.

    The wording is not cosmetic. "Your role is published at <url>" and "we obtained
    your details from a contact database" are different factual claims, and only one
    is true for a broker record. Saying the first about the second would be a false
    statement in a legally-required disclosure.
    """
    if provenance_kind == "primary_source":
        origin = f"your professional role is published at {source_url}"
    elif provenance_kind == "broker_attested":
        name = provider or "a third-party business contact database"
        origin = (
            f"we obtained your business contact details from {name}, a B2B contact "
            "database, rather than from you directly"
        )
    else:
        origin = f"your business contact details were recorded from {source_url}"

    return (
        "\n\n---\n"
        f"You are receiving this because {origin}. We contact you in your professional "
        "capacity only, on the basis of legitimate interest.\n"
        # The literal word matters: recipients scan for it and filters look for it,
        # which is why _UNSUBSCRIBE_PATTERN insists on it rather than trusting intent.
        f"Unsubscribe and have your details deleted: {unsubscribe_url}\n"
        f"{postal_address}"
    )
