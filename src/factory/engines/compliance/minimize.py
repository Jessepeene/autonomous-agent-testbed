"""GDPR Art. 5(1)(c) data minimisation, enforced at the database boundary.

Scrapers return whatever a page happens to contain — phone numbers, personal
addresses, LinkedIn IDs, photos. None of that is necessary to email someone about a
B2B tool, so none of it is permitted to reach storage.

``minimize()`` is the only sanctioned path into the ``leads`` table. It is a pure
function with no I/O so it can be property-tested against arbitrary scraped input.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from factory.state.schemas import ALLOWED_LEAD_FIELDS, LeadRecord


class ProvenanceMissing(ValueError):
    """A lead without verifiable origin cannot be stored, let alone contacted."""


def dropped_fields(raw: Mapping[str, Any]) -> set[str]:
    """Everything the caller supplied that we refuse to persist. For audit logging."""
    permitted = ALLOWED_LEAD_FIELDS | {"source_url", "scraped_at"}
    return {key for key in raw if key not in permitted}


def minimize(raw: Mapping[str, Any]) -> LeadRecord:
    """Reduce arbitrary scraped data to the five permitted PII fields plus provenance.

    Raises:
        ProvenanceMissing: when ``source_url`` is absent — proof-of-origin is not
            optional, so a lead that cannot be traced is discarded rather than stored.
        ValueError: when a required field is missing or malformed.
    """
    source_url = str(raw.get("source_url") or "").strip()
    if not source_url:
        raise ProvenanceMissing("source_url is required for every stored lead")

    scraped_at = raw.get("scraped_at") or datetime.now(UTC)
    if isinstance(scraped_at, str):
        scraped_at = datetime.fromisoformat(scraped_at)
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=UTC)

    payload: dict[str, Any] = {field: raw.get(field) for field in ALLOWED_LEAD_FIELDS}
    missing = [key for key, value in payload.items() if value in (None, "")]
    if missing:
        raise ValueError(f"lead is missing required fields: {', '.join(sorted(missing))}")

    payload["source_url"] = source_url
    payload["scraped_at"] = scraped_at
    return LeadRecord(**payload)
