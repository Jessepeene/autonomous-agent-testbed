"""DISCOVERY_SCOUT's one tool: hand a found market signal back to the pipeline.

The fixed sources (`engines/discovery/sources/`) return `Signal` objects directly
from a plain HTTP call -- there is no agent step and nothing to register as a
tool. DISCOVERY_SCOUT is different: it is a genuine agent, free to search
wherever it judges useful (`http` category), so its findings have to cross back
into the run via a tool call rather than a function return value, exactly like
CHANNEL_SCOUT's `propose_channel` (`tools/growth.py`). Unlike that tool, nothing
here is mutating in the human-facing sense -- a signal is raw evidence, not a
decision -- so it needs no approval interrupt, just a durable row the calling
cycle reads back (`state/repository.py::drain_scout_signals`).
"""

from __future__ import annotations

from typing import Any

from factory.logging import get_logger
from factory.settings import get_settings
from factory.tools.registry import REGISTRY

log = get_logger(__name__)


@REGISTRY.tool(
    name="submit_signal",
    description=(
        "Record one real, specific piece of evidence that someone has an unmet "
        "need -- a complaint, a manual workaround, a 'wish there was a tool for "
        "this' -- that you actually read. Quote the real text (a real comment, "
        "post, or review) and give its exact URL; never paraphrase into a vague "
        "claim and never invent a URL. Call this once per distinct piece of "
        "evidence, as many times as you find real ones."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The exact task id given to you at the start of this run.",
            },
            "title": {
                "type": "string",
                "maxLength": 300,
                "description": "The thread/post/review title, or a short label if it has none.",
            },
            "quote": {
                "type": "string",
                "maxLength": 4000,
                "description": "The real text you found, verbatim or lightly trimmed.",
            },
            "url": {
                "type": "string",
                "maxLength": 500,
                "description": "The exact URL you read this at.",
            },
            "source_label": {
                "type": "string",
                "maxLength": 60,
                "description": "Where this came from, e.g. 'indiehackers', 'g2_reviews', 'devto'.",
            },
            "engagement": {
                "type": "integer",
                "description": (
                    "A rough popularity signal if the page shows one (upvotes, replies, "
                    "reactions) -- 0 if none is visible. Do not guess a number."
                ),
            },
        },
        "required": ["task_id", "title", "quote", "url", "source_label"],
        "additionalProperties": False,
    },
    category="discovery_scout",
)
async def submit_signal(
    task_id: str,
    title: str,
    quote: str,
    url: str,
    source_label: str,
    engagement: int = 0,
) -> dict[str, Any]:
    from factory.state import repository
    from factory.state.db import session_scope

    if not url.startswith(("http://", "https://")):
        return {"error": "url must be absolute http(s)"}

    settings = get_settings()
    async with session_scope(settings) as session:
        signal_id = await repository.record_scout_signal(
            session,
            task_id=task_id,
            title=title,
            body=quote,
            url=url,
            source=source_label,
            engagement=engagement,
        )
    return {"recorded": True, "signal_id": signal_id}
