"""``0028_tier0_prompt_appendices``'s data, in isolation from a live DB.

Same reasoning and same loading mechanism as `test_idea_defensibility_migration.py`
(the migration file can't be `import`ed by dotted path since its module name
starts with a digit).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from factory.engines.discovery.score import DISCRIMINATOR_SYSTEM
from factory.engines.marketing.outreach import SYSTEM_PROMPT as OUTREACH_SYSTEM_PROMPT

_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0028_tier0_prompt_appendices.py"
)
_GENERALISATION_MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "0047_discriminator_example_generalisation.py"
)


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_migration_0028", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generalisation_migration() -> ModuleType:
    """0047 is the next migration after this one to touch the discriminator key --
    needed here so the suffix check below accounts for every migration that has
    touched it since, not just this one."""
    spec = importlib.util.spec_from_file_location(
        "_migration_0047", _GENERALISATION_MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discriminator_appendix_is_a_real_suffix_of_the_live_constant(
    migration: ModuleType, generalisation_migration: ModuleType
) -> None:
    """The exact invariant this migration exists for: appending this text to
    whatever a database still has active must reproduce today's real prompt --
    checked here the other way around, that the live constant actually ends
    with this exact appendix, immediately followed by whatever 0047 later
    appended after it. This migration's own text is no longer the final suffix
    by itself once a later one exists; the combination still must be."""
    assert DISCRIMINATOR_SYSTEM.endswith(
        migration._DISCRIMINATOR_APPENDIX + generalisation_migration._GENERALISATION_APPENDIX
    )


def test_outreach_appendix_is_a_real_suffix_of_the_live_constant(migration: ModuleType) -> None:
    assert OUTREACH_SYSTEM_PROMPT.endswith(migration._OUTREACH_APPENDIX)


def test_discriminator_appendix_names_the_new_field(migration: ModuleType) -> None:
    assert "gut_check_passed" in migration._DISCRIMINATOR_APPENDIX
    assert "gut_check_reasoning" in migration._DISCRIMINATOR_APPENDIX


def test_outreach_appendix_names_the_new_field(migration: ModuleType) -> None:
    assert "reads_like_a_real_person_wrote_it" in migration._OUTREACH_APPENDIX
    assert "tone_concern" in migration._OUTREACH_APPENDIX


def test_append_to_active_row_is_idempotent_on_an_already_appended_row() -> None:
    """A real regression this codebase already worries about elsewhere
    (`0023`'s own "must not double-append" concern, made explicit here): a
    row already ending with the appendix must not receive it a second time --
    e.g. if this migration is ever re-run, or a later migration's data
    accidentally overlaps."""
    from unittest.mock import MagicMock

    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import JSONB

    spec = importlib.util.spec_from_file_location("_migration_0028_exec", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    tunable = sa.table(
        "tunable_parameters",
        sa.column("id", sa.String),
        sa.column("key", sa.String),
        sa.column("value", JSONB),
        sa.column("status", sa.String),
    )
    already_appended_text = "base text" + module._DISCRIMINATOR_APPENDIX
    conn = MagicMock()
    conn.execute.return_value.first.return_value = MagicMock(
        id="row-1", value={"text": already_appended_text}
    )

    module._append_to_active_row(conn, tunable, "some-key", module._DISCRIMINATOR_APPENDIX)

    # Only the SELECT ran -- no UPDATE was ever issued for an already-appended row.
    assert conn.execute.call_count == 1
