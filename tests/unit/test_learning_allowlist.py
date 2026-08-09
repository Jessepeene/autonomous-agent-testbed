"""The hard allowlist — the one thing standing between "the factory tunes its own
marketing copy" and "the factory tunes its own legal compliance prompts."

`bootstrap_active` and `insert_candidate` call `assert_mutable(key)` as their literal
first statement, before touching the database at all, so rejection is testable with no
session and no Postgres — the guard fires before there is anything to connect to.
"""

from __future__ import annotations

import pytest

from factory.learning import store
from factory.learning.allowlist import (
    DEFAULT_METRIC,
    MUTABLE_KEYS,
    ProtectedKey,
    assert_mutable,
    default_metric_for,
)

PROTECTED_KEYS = [
    "factory.engines.compliance.assessor:LIA_SYSTEM_PROMPT",
    "factory.engines.compliance.assessor:LIA_BATCH_SYSTEM_PROMPT",
    "factory.engines.compliance.assessor:IDEA_SYSTEM_PROMPT",
    "factory.engines.development.bug_council:REVIEWER_PROMPTS",
    "factory.supervisor.orchestrator:_SYSTEM_PROMPT_TEMPLATE",
    "factory.engines.development.scaffold.generator:SYSTEM_PROMPT",
]


@pytest.mark.parametrize("key", PROTECTED_KEYS)
def test_every_protected_key_is_rejected(key: str) -> None:
    with pytest.raises(ProtectedKey):
        assert_mutable(key)


@pytest.mark.parametrize("key", sorted(MUTABLE_KEYS))
def test_every_allowlisted_key_is_accepted(key: str) -> None:
    assert_mutable(key)  # must not raise


def test_the_two_same_named_system_prompt_constants_do_not_collide() -> None:
    """The exact collision this allowlist exists to resolve: two unrelated modules
    both define a constant literally named SYSTEM_PROMPT."""
    outreach_key = "factory.engines.marketing.outreach:SYSTEM_PROMPT"
    codegen_key = "factory.engines.development.scaffold.generator:SYSTEM_PROMPT"
    assert_mutable(outreach_key)
    with pytest.raises(ProtectedKey):
        assert_mutable(codegen_key)


def test_every_mutable_key_has_exactly_one_default_metric() -> None:
    assert set(DEFAULT_METRIC) == MUTABLE_KEYS
    for key in MUTABLE_KEYS:
        assert isinstance(default_metric_for(key), str)


@pytest.mark.parametrize("key", PROTECTED_KEYS)
async def test_bootstrap_active_rejects_a_protected_key_before_touching_the_database(
    key: str,
) -> None:
    with pytest.raises(ProtectedKey):
        await store.bootstrap_active(
            None,  # type: ignore[arg-type]
            kind="PROMPT",
            key=key,
            value={"text": "x"},
            created_by="test",
            target_metric="idea_survival_rate",
        )


@pytest.mark.parametrize("key", PROTECTED_KEYS)
async def test_insert_candidate_rejects_a_protected_key_before_touching_the_database(
    key: str,
) -> None:
    with pytest.raises(ProtectedKey):
        await store.insert_candidate(
            None,  # type: ignore[arg-type]
            key=key,
            kind="PROMPT",
            value={"text": "x"},
            rationale="x" * 30,
            created_by="agent:analyst",
            parent_id=None,
            target_metric="idea_survival_rate",
        )
