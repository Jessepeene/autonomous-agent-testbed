"""``0023_idea_defensibility``'s data-rewrite logic, in isolation from a live DB.

The migration file itself can't be `import`ed by dotted path (its module name
starts with a digit, same as every other file in `alembic/versions/`), and this
codebase has no established pattern for running an Alembic migration's
`upgrade()` against a real connection outside the CLI -- the existing
scratch-Postgres pipeline exercises this migration structurally (columns/
constraints created, no crash) but a truly fresh scratch database has no
pre-existing `tunable_parameters` rows, so it never actually exercises the
interesting branch: rewriting a row that's already there.

So the branching *decisions* -- is this row still the untouched default, and if
not, does rescaling preserve the sum-to-one contract -- are pulled out into
plain functions (`_matches`, `_rescale_learned_weights`) and tested directly
here, loaded via `importlib` since a normal `import` statement can't reference
this filename.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from factory.engines.discovery.score import DISCRIMINATOR_SYSTEM
from factory.state.schemas import ScoredIdea

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0023_idea_defensibility.py"
)
_APPENDICES_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0028_tier0_prompt_appendices.py"
)
_GENERALISATION_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0047_discriminator_example_generalisation.py"
)


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load(_MIGRATION_PATH, "_migration_0023")


@pytest.fixture(scope="module")
def later_appendices_migration() -> ModuleType:
    """0028 is the next migration to touch this same tunable key -- see its own
    module docstring. Needed here only to compose the full no-drift check
    below across both migrations combined."""
    return _load(_APPENDICES_MIGRATION_PATH, "_migration_0028")


@pytest.fixture(scope="module")
def generalisation_migration() -> ModuleType:
    """0047 is the next migration after 0028 to touch this same key -- see its own
    module docstring. Needed here for the same reason 0028's fixture is: the
    no-drift check below composes every migration that has ever touched this
    prompt, not just the first two."""
    return _load(_GENERALISATION_MIGRATION_PATH, "_migration_0047")


# --- _matches: detecting an untouched row --------------------------------------


def test_matches_the_old_default_exactly(migration: ModuleType) -> None:
    assert migration._matches(migration._OLD_DEFAULT_WEIGHTS, migration._OLD_DEFAULT_WEIGHTS)


def test_matches_rejects_a_learned_value(migration: ModuleType) -> None:
    learned = dict(migration._OLD_DEFAULT_WEIGHTS)
    learned["pain_severity"] = 0.4
    assert not migration._matches(learned, migration._OLD_DEFAULT_WEIGHTS)


def test_matches_rejects_a_superset_of_keys(migration: ModuleType) -> None:
    """A row that already carries `defensibility` (e.g. a re-run, or a partially
    applied prior attempt) must not be mistaken for the untouched 4-key default."""
    extra_key = {**migration._OLD_DEFAULT_WEIGHTS, "defensibility": 0.0}
    assert not migration._matches(extra_key, migration._OLD_DEFAULT_WEIGHTS)


def test_matches_rejects_a_subset_of_keys(migration: ModuleType) -> None:
    missing_key = dict(migration._OLD_DEFAULT_WEIGHTS)
    del missing_key["adoption_ease"]
    assert not migration._matches(missing_key, migration._OLD_DEFAULT_WEIGHTS)


def test_matches_tolerates_negligible_floating_point_noise(migration: ModuleType) -> None:
    noisy = {k: v + 1e-12 for k, v in migration._OLD_DEFAULT_WEIGHTS.items()}
    assert migration._matches(noisy, migration._OLD_DEFAULT_WEIGHTS)


# --- _rescale_learned_weights: preserving ratios while making room -------------


def test_rescale_sums_to_one_with_defensibility_inserted(migration: ModuleType) -> None:
    learned = {
        "pain_severity": 0.4,
        "willingness_to_pay": 0.3,
        "adoption_ease": 0.2,
        "build_feasibility": 0.1,
    }
    rescaled = migration._rescale_learned_weights(learned)
    assert rescaled["defensibility"] == migration._DEFENSIBILITY_WEIGHT
    assert round(sum(rescaled.values()), 6) == 1.0


def test_rescale_preserves_relative_ratios_between_original_dimensions(
    migration: ModuleType,
) -> None:
    """The whole point of scaling by a uniform factor rather than, say,
    subtracting a flat amount from each: the learning loop's discovered *relative*
    priorities (pain matters 4x more than feasibility) must survive unchanged."""
    learned = {
        "pain_severity": 0.4,
        "willingness_to_pay": 0.3,
        "adoption_ease": 0.2,
        "build_feasibility": 0.1,
    }
    rescaled = migration._rescale_learned_weights(learned)
    original_ratio = learned["pain_severity"] / learned["build_feasibility"]
    rescaled_ratio = rescaled["pain_severity"] / rescaled["build_feasibility"]
    assert round(original_ratio, 6) == round(rescaled_ratio, 6)


def test_rescale_scales_every_original_weight_by_the_documented_factor(
    migration: ModuleType,
) -> None:
    learned = {
        "pain_severity": 0.4,
        "willingness_to_pay": 0.3,
        "adoption_ease": 0.2,
        "build_feasibility": 0.1,
    }
    rescaled = migration._rescale_learned_weights(learned)
    for dim, original in learned.items():
        assert rescaled[dim] == round(original * migration._LEARNED_WEIGHT_SCALE, 6)


# --- The new default weights themselves -----------------------------------------


def test_new_default_weights_sum_to_one_and_match_the_code_constant(
    migration: ModuleType,
) -> None:
    assert round(sum(migration._NEW_DEFAULT_WEIGHTS.values()), 6) == 1.0
    assert migration._NEW_DEFAULT_WEIGHTS == ScoredIdea.default_weights()


def test_old_default_weights_do_not_sum_to_one_plus_defensibility(migration: ModuleType) -> None:
    """Sanity check on the constant itself: the old default's total, scaled by
    0.82, plus 0.18, must land back at 1.0 -- if this ever failed, the scaling
    factor and the defensibility weight have drifted out of sync with each other."""
    old_total = sum(migration._OLD_DEFAULT_WEIGHTS.values())
    assert round(old_total, 6) == 1.0
    assert (
        round(old_total * migration._LEARNED_WEIGHT_SCALE + migration._DEFENSIBILITY_WEIGHT, 6)
        == 1.0
    )


# --- The discriminator prompt text: must never silently drift from the code ----


def test_new_discriminator_system_matches_the_live_code_constant(
    migration: ModuleType,
    later_appendices_migration: ModuleType,
    generalisation_migration: ModuleType,
) -> None:
    """The migration carries its own copy of the post-Phase-4 prompt text (Alembic
    migrations must not import application code that might itself change later)
    -- this is the guard that the copy can never silently drift from the real
    constant `discovery/graph.py` actually serves.

    0023 is no longer the last migration to touch this key -- 0028 appended a
    Tier 0 holistic-check paragraph (system-wide verification plan), and 0047
    later appended a generalising clause to the adoption-ease examples (Neutral
    prompting, 2026-09-27) -- so the true invariant is now "0023's text plus
    every later appendix equals today's live constant," not "0023's text alone
    still does." Composing all three here keeps this a real no-drift guard
    instead of a stale assumption that quietly stopped being checked the moment
    a second, and now a third, migration touched this key.
    """
    assert (
        migration._NEW_DISCRIMINATOR_SYSTEM
        + later_appendices_migration._DISCRIMINATOR_APPENDIX
        + generalisation_migration._GENERALISATION_APPENDIX
        == DISCRIMINATOR_SYSTEM
    )


def test_discriminator_appendix_names_every_new_field(migration: ModuleType) -> None:
    new_fields = (
        "defensibility_reasoning",
        "defensibility",
        "platform_dependency",
        "replacement_mode",
    )
    for field in new_fields:
        assert field in migration._DISCRIMINATOR_APPENDIX


def test_old_and_new_discriminator_system_are_different_strings(migration: ModuleType) -> None:
    assert migration._OLD_DISCRIMINATOR_SYSTEM != migration._NEW_DISCRIMINATOR_SYSTEM
