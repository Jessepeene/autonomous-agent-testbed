"""Autonomous legal-entity resolution — the pure, DB-free pieces.

`resolve_entity_type`'s caching (`get_entity_verdict`/`store_entity_verdict`) is
Postgres-specific (`ON CONFLICT DO NOTHING`) and covered by an integration test. What
matters most at the unit level is the fail-closed property itself: an agent or
provider failure, or a genuinely inconclusive investigation, must never resolve to
something that passes the gate.
"""

from __future__ import annotations

from factory.engines.compliance.entity import _unknown, passes_entity_gate
from factory.settings import Settings
from factory.state.schemas import EntityType, EntityVerdict


def _verdict(**overrides: object) -> EntityVerdict:
    base: dict[str, object] = {
        "entity_type": EntityType.CORPORATE,
        "confidence": 0.9,
        "evidence": "The company website's imprint names 'Analytical Engines B.V.'",
    }
    base.update(overrides)
    return EntityVerdict(**base)  # type: ignore[arg-type]


def test_unknown_never_passes(settings: Settings) -> None:
    verdict = _verdict(entity_type=EntityType.UNKNOWN, confidence=0.0)
    assert passes_entity_gate(verdict, settings=settings) is False


def test_sole_proprietor_never_passes_even_at_high_confidence(settings: Settings) -> None:
    verdict = _verdict(entity_type=EntityType.SOLE_PROPRIETOR_OR_INDIVIDUAL, confidence=0.95)
    assert passes_entity_gate(verdict, settings=settings) is False


def test_corporate_below_min_confidence_does_not_pass(settings: Settings) -> None:
    below = settings.entity_resolution_min_confidence - 0.01
    verdict = _verdict(entity_type=EntityType.CORPORATE, confidence=below)
    assert passes_entity_gate(verdict, settings=settings) is False


def test_corporate_at_min_confidence_passes(settings: Settings) -> None:
    verdict = _verdict(
        entity_type=EntityType.CORPORATE, confidence=settings.entity_resolution_min_confidence
    )
    assert passes_entity_gate(verdict, settings=settings) is True


def test_corporate_at_high_confidence_passes(settings: Settings) -> None:
    verdict = _verdict(entity_type=EntityType.CORPORATE, confidence=1.0)
    assert passes_entity_gate(verdict, settings=settings) is True


def test_unknown_helper_never_produces_a_passing_verdict(settings: Settings) -> None:
    """Whatever the reason string, `_unknown(...)` is the module's universal
    fail-closed fallback and must always fail the gate."""
    verdict = _unknown("the sandboxed fetch timed out")
    assert verdict.entity_type is EntityType.UNKNOWN
    assert verdict.confidence == 0.0
    assert passes_entity_gate(verdict, settings=settings) is False
