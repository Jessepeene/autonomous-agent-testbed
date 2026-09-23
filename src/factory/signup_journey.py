"""What a visitor sees after they leave their email address, and who gets to change
it for one product without changing it for every product.

The confirmation email and the page its link leads to were constants in `api/app.py`,
byte-identical for every product this factory has ever made -- factory infrastructure,
not something any agent could author or change. A pre-ship reviewer can read the mail
it sent and follow the confirmation link to the page behind it (`preship.py`'s
"you are reviewing the journey, not only the page"), and until this had nothing to
change on either, because neither belonged to any product. Run 15's reviewer opened
the exact confirmation page Jesse then screenshotted, and reported nothing -- it had
genuinely looked, and found nothing it was allowed to touch.

`Product.signup_journey` is the lever, and it is deliberately empty for every product
until a reviewer actually flags the generic version as a problem
(`engines/development/signup_journey.py` is where that patch is written). This module
is the other half: rendering. It never talks to a model or the database -- given a
product's overrides (or none), it returns the text to show, falling back to today's
wording for anything not yet overridden. Kept pure and separate from `api/app.py` so
the fallback behaviour is testable without a running server, and separate from the
engine module that writes overrides so a rendering bug can never be mistaken for a
patch-generation bug.

Does not cover `/u/{token}` (GDPR erasure). See `Product.signup_journey`'s own
docstring for why that page stays generic on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The three pieces a product may override. A key absent from a product's
#: `signup_journey` means "use the fallback below" -- never an error, never a reason
#: to leave a field blank in what actually gets sent or shown.
CONFIRMATION_EMAIL_SUBJECT = "confirmation_email_subject"
CONFIRMATION_EMAIL_BODY = "confirmation_email_body"
CONFIRMATION_PAGE_TEXT = "confirmation_page_text"

_DEFAULT_SUBJECT = "Please confirm your interest"


def _default_body(confirm_link: str) -> str:
    return (
        "Thanks for your interest — please confirm by visiting the link below. "
        f"If you didn't request this, you can ignore it.\n\n{confirm_link}"
    )


def _default_page_text(unsubscribe_path: str) -> str:
    return (
        "Thanks — your interest has been confirmed. If you'd rather not be "
        f"contacted about this again, visit {unsubscribe_path} to have your "
        "details deleted."
    )


@dataclass(frozen=True, slots=True)
class ConfirmationEmail:
    subject: str
    body: str


def render_confirmation_email(
    signup_journey: dict[str, str], *, confirm_link: str
) -> ConfirmationEmail:
    """The subject and body to send, this product's own if it has one.

    `confirm_link` is never something an override can replace -- it is composed
    here, appended to whatever body text applies, so a stored override can never go
    stale by holding a token-shaped link that isn't this submission's token.
    """
    subject = signup_journey.get(CONFIRMATION_EMAIL_SUBJECT) or _DEFAULT_SUBJECT
    body = signup_journey.get(CONFIRMATION_EMAIL_BODY)
    if body:
        return ConfirmationEmail(subject=subject, body=f"{body}\n\n{confirm_link}")
    return ConfirmationEmail(subject=subject, body=_default_body(confirm_link))


def render_confirmation_page_text(signup_journey: dict[str, str], *, unsubscribe_path: str) -> str:
    """What the visitor reads after clicking the confirmation link.

    `unsubscribe_path` is appended the same way the confirm link is above --
    composed fresh from this visitor's own token, never something an override
    holds a stale copy of.
    """
    text = signup_journey.get(CONFIRMATION_PAGE_TEXT)
    if text:
        return f"{text}\n\n{unsubscribe_path}"
    return _default_page_text(unsubscribe_path)
