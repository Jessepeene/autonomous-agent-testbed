"""Rendering the confirmation email and page: this product's own text if a reviewer
has ever flagged the generic version, today's wording otherwise.

Pure functions, no database, no model call -- kept that way in
`factory/signup_journey.py` specifically so the fallback behaviour (which is what
every product gets until anyone ever changes anything) is testable without either.
"""

from __future__ import annotations

from factory.signup_journey import (
    CONFIRMATION_EMAIL_BODY,
    CONFIRMATION_EMAIL_SUBJECT,
    CONFIRMATION_PAGE_TEXT,
    render_confirmation_email,
    render_confirmation_page_text,
)


def test_an_empty_override_renders_exactly_todays_wording() -> None:
    """The behaviour every product must have until this feature ever does anything
    -- a byte-for-byte match to what shipped before `Product.signup_journey`
    existed, since a rendering bug here would silently change every product's
    email at once."""
    mail = render_confirmation_email({}, confirm_link="https://x.test/confirm/abc")
    assert mail.subject == "Please confirm your interest"
    assert mail.body == (
        "Thanks for your interest — please confirm by visiting the link below. "
        "If you didn't request this, you can ignore it.\n\nhttps://x.test/confirm/abc"
    )

    page = render_confirmation_page_text({}, unsubscribe_path="/u/abc")
    assert page == (
        "Thanks — your interest has been confirmed. If you'd rather not be "
        "contacted about this again, visit /u/abc to have your details deleted."
    )


def test_an_override_replaces_only_the_field_it_names() -> None:
    """A product with a subject override and nothing else keeps the factory's own
    body wording -- an override is a per-field fact, not an all-or-nothing switch."""
    mail = render_confirmation_email(
        {CONFIRMATION_EMAIL_SUBJECT: "Confirm your Flakebreak signup"},
        confirm_link="https://x.test/confirm/abc",
    )
    assert mail.subject == "Confirm your Flakebreak signup"
    assert "Thanks for your interest" in mail.body


def test_the_confirm_link_is_never_something_an_override_can_supply() -> None:
    """A stored override cannot go stale by holding a token-shaped link from a
    different visitor -- the real link for *this* click is always appended fresh,
    never taken from the override text."""
    mail = render_confirmation_email(
        {CONFIRMATION_EMAIL_BODY: "Hi from Flakebreak! Please confirm."},
        confirm_link="https://x.test/confirm/THIS-VISITORS-TOKEN",
    )
    assert mail.body.endswith("https://x.test/confirm/THIS-VISITORS-TOKEN")
    assert mail.body.startswith("Hi from Flakebreak! Please confirm.")


def test_the_erasure_link_is_never_something_an_override_can_supply() -> None:
    """Same guarantee for the confirmation page -- an agent-written override gets
    a working, unfakeable path to delete their data appended after it, always."""
    page = render_confirmation_page_text(
        {CONFIRMATION_PAGE_TEXT: "You're in! We'll be in touch."},
        unsubscribe_path="/u/THIS-VISITORS-TOKEN",
    )
    assert page.endswith("/u/THIS-VISITORS-TOKEN")
    assert page.startswith("You're in! We'll be in touch.")


def test_an_empty_string_override_is_treated_as_no_override() -> None:
    """A merge that ever wrote "" for a field a patch chose not to touch must not
    render as an empty confirmation page -- falsy is falsy, whatever put it there."""
    page = render_confirmation_page_text({CONFIRMATION_PAGE_TEXT: ""}, unsubscribe_path="/u/x")
    assert "Thanks — your interest has been confirmed" in page
