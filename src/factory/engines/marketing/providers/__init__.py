"""Lead providers."""

from __future__ import annotations

from factory.engines.marketing.providers.apollo import ApolloProvider
from factory.engines.marketing.providers.base import (
    LeadCandidate,
    LeadProvider,
    LeadSearchPlan,
    Provenance,
    ProvenanceKind,
    ProviderAuthError,
    ProviderRequestError,
)
from factory.engines.marketing.providers.csv_drop import CsvDropProvider
from factory.engines.marketing.providers.hunter import HunterProvider
from factory.settings import Settings, get_settings

__all__ = [
    "ApolloProvider",
    "CsvDropProvider",
    "HunterProvider",
    "LeadCandidate",
    "LeadProvider",
    "LeadSearchPlan",
    "Provenance",
    "ProvenanceKind",
    "ProviderAuthError",
    "ProviderRequestError",
    "build_providers",
]


def build_providers(settings: Settings | None = None) -> list[LeadProvider]:
    """Every *autonomous* data-provider adapter that is currently usable.

    Hunter first: it is the only source that can supply a primary URL, and a record
    with real provenance produces a stronger Art. 14 disclosure than a broker record
    for the same person.

    ``CsvDropProvider`` is deliberately excluded: the manual CSV path is deprecated as
    the Marketing Engine's autonomous sourcing route (see
    `leads.py::refine_sourcing_strategy`) — an operator can still invoke
    `CsvDropProvider` directly for a personally-vetted list, but it is no longer part
    of the pipeline `ingest()` runs on its own initiative.
    """
    settings = settings or get_settings()
    candidates: list[LeadProvider] = [HunterProvider(settings), ApolloProvider(settings)]
    return [provider for provider in candidates if provider.available]
