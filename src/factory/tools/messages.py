"""Letting a reviewer read what this company actually sent.

The missing third of "expand what it is allowed to see". `sent_messages` retains
every composed message and `allowed_origins_for` lets a review follow a link onto
the API host -- but neither is any use while nothing can *read* the message, because
the confirmation link a visitor clicks only exists inside the mail. Without this the
reviewer submits the form, a real token is issued, and it has no way to learn what
the token is.

Pinned to one product, the same as `report_product_issue` and the browser tools: a
review of one product has no business reading what was written to somebody else's
customers, and these rows hold real addresses.

Addresses are redacted to their domain. A reviewer judging whether a message reads
like a real company wrote it needs the subject, the sender and the words; it never
needs to know who received them, and a review transcript is one more place personal
data would otherwise end up.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from factory.logging import get_logger
from factory.state.db import session_scope
from factory.state.orm import SentMessage
from factory.tools.registry import ToolDescriptor, ToolRegistry

log = get_logger(__name__)

#: Enough to judge a message, bounded so a chatty product cannot fill the context.
_LIMIT = 10
_BODY_CHARS = 4000


def _redact(address: str) -> str:
    """`someone@example.com` -> `<recipient>@example.com`."""
    _, _, domain = address.partition("@")
    return f"<recipient>@{domain}" if domain else "<recipient>"


def register_message_tools(registry: ToolRegistry, *, product_id: str) -> None:
    """Add `read_sent_messages`, pinned to this product."""

    async def read_sent_messages() -> dict[str, Any]:
        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(SentMessage)
                        .where(SentMessage.product_id == product_id)
                        .order_by(SentMessage.created_at.desc())
                        .limit(_LIMIT)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "count": len(rows),
            "messages": [
                {
                    "sent_at": r.created_at.isoformat(),
                    # "we sent this" and "we would have sent this" are different facts.
                    "outcome": r.outcome,
                    "held_because": r.detail,
                    "from": r.from_address,
                    "to": _redact(r.to_address),
                    "subject": r.subject,
                    "body": r.body[:_BODY_CHARS],
                }
                for r in rows
            ],
        }

    registry.register(
        ToolDescriptor(
            name="read_sent_messages",
            description=(
                "Read the emails this company sent about this product -- subject, "
                "sender and full text, exactly as a recipient received them. Use it "
                "after submitting the signup form: the confirmation message is what a "
                "real visitor gets next, and any link in it is part of this product's "
                "journey, so you can open it in the browser and see where it leads. "
                "Recipients are redacted; nothing else is. A message marked "
                "'composed_only' was written but deliberately not transmitted."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            category="browser",
            handler=read_sent_messages,
        )
    )


def confirmation_links(body: str) -> list[str]:
    """Every absolute URL in a message body, for a caller that wants them directly."""
    return re.findall(r"https?://[^\s<>\"')]+", body)
