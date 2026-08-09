"""Operator-supplied CSV drop.

Retained as a manual override now that the API providers do the autonomous work. It
is still the right path for a list a human has personally vetted — a conference
attendee list, an existing customer base, inbound sign-ups — where the operator, not
a broker, is the one vouching for the record.

Rows must carry their own ``source_url``: a human dropping a file still has to say
where the data came from.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from factory.engines.marketing.providers.base import (
    LeadCandidate,
    LeadSearchPlan,
    Provenance,
    ProvenanceKind,
)
from factory.logging import get_logger
from factory.settings import Settings, get_settings

log = get_logger(__name__)

LEAD_DROP_DIRNAME = "lead-drops"
REQUIRED_COLUMNS = frozenset(
    {"first_name", "last_name", "job_title", "company", "work_email", "source_url"}
)


def drop_directory(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    path = settings.workspace_root / LEAD_DROP_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def iter_rows(settings: Settings | None = None) -> Iterator[tuple[Path, dict[str, str]]]:
    for csv_path in sorted(drop_directory(settings).glob("*.csv")):
        try:
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
                if missing:
                    log.warning("leads.csv_rejected", file=csv_path.name, missing=sorted(missing))
                    continue
                for row in reader:
                    yield csv_path, row
        except OSError as exc:
            log.warning("leads.csv_unreadable", file=csv_path.name, detail=str(exc)[:200])


class CsvDropProvider:
    name = "operator-csv"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def available(self) -> bool:
        return any(drop_directory(self._settings).glob("*.csv"))

    async def search(self, plan: LeadSearchPlan, *, limit: int = 25) -> list[LeadCandidate]:
        """The plan is ignored: an operator's file is already the intended audience."""
        candidates: list[LeadCandidate] = []
        for _, row in iter_rows(self._settings):
            if len(candidates) >= limit:
                break
            candidate = self._to_candidate(row)
            if candidate is not None:
                candidates.append(candidate)
        log.info("leads.csv_results", count=len(candidates))
        return candidates

    def _to_candidate(self, row: dict[str, str]) -> LeadCandidate | None:
        source_url = (row.get("source_url") or "").strip()
        if not source_url.startswith(("http://", "https://")):
            return None
        fields = {
            key: (row.get(key) or "").strip()
            for key in ("first_name", "last_name", "job_title", "company", "work_email")
        }
        if not all(fields.values()):
            return None

        scraped_raw = (row.get("scraped_at") or "").strip()
        try:
            scraped_at = datetime.fromisoformat(scraped_raw) if scraped_raw else datetime.now(UTC)
        except ValueError:
            scraped_at = datetime.now(UTC)
        if scraped_at.tzinfo is None:
            scraped_at = scraped_at.replace(tzinfo=UTC)

        return LeadCandidate(
            first_name=fields["first_name"],
            last_name=fields["last_name"],
            job_title=fields["job_title"],
            company=fields["company"],
            work_email=fields["work_email"].lower(),
            provenance=Provenance(
                kind=ProvenanceKind.OPERATOR_SUPPLIED,
                url=source_url,
                provider=self.name,
                retrieved_at=scraped_at,
            ),
        )
