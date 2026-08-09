"""Learning loop: tunable parameters, episodes, heal attempts, outreach attribution.

Revision ID: 0005_learning_loop
Revises: 0004_ledger_product_attribution
Create Date: 2026-08-08

Three new tables plus two column additions, all in service of one goal: the factory
can propose, trial, and keep-or-discard changes to a small allowlisted set of prompts
and scoring weights, from real outcome data, without a human in the loop for each
change. See `src/factory/learning/` for the code that reads and writes these tables —
`tunable_parameters` may only ever be written by `learning/store.py`, which enforces
the allowlist in `learning/allowlist.py` on every write.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_learning_loop"
down_revision: str | None = "0004_ledger_product_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tunable_parameters (
            id VARCHAR(32) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            key VARCHAR(200) NOT NULL,
            value JSONB NOT NULL,
            version INTEGER NOT NULL,
            status VARCHAR(16) NOT NULL,
            target_metric VARCHAR(64) NOT NULL,
            rationale TEXT NOT NULL,
            created_by VARCHAR(64) NOT NULL,
            parent_id VARCHAR(32),
            trial_started_at TIMESTAMP WITH TIME ZONE,
            trial_until TIMESTAMP WITH TIME ZONE,
            promoted_at TIMESTAMP WITH TIME ZONE,
            retired_at TIMESTAMP WITH TIME ZONE,
            retired_reason VARCHAR(500),
            trial_numerator INTEGER,
            trial_denominator INTEGER,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_tunable_parameters PRIMARY KEY (id),
            CONSTRAINT ck_tunable_parameters_tunable_kind_valid
                CHECK (kind IN ('PROMPT','SCORING_WEIGHTS')),
            CONSTRAINT ck_tunable_parameters_tunable_status_valid
                CHECK (status IN ('candidate','trial','active','retired')),
            CONSTRAINT uq_tunable_parameters_key_version UNIQUE (key, version),
            CONSTRAINT fk_tunable_parameters_parent_id_tunable_parameters
                FOREIGN KEY(parent_id) REFERENCES tunable_parameters (id)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_tunable_parameters_active ON tunable_parameters (key) "
        "WHERE status = 'active'"
    )
    op.execute("CREATE INDEX ix_tunable_parameters_key_status ON tunable_parameters (key, status)")

    op.execute(
        """
        CREATE TABLE learning_episodes (
            id VARCHAR(32) NOT NULL,
            tunable_parameter_id VARCHAR(32) NOT NULL,
            outcome VARCHAR(16) NOT NULL,
            detail JSONB NOT NULL,
            task_id VARCHAR(32),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            CONSTRAINT pk_learning_episodes PRIMARY KEY (id),
            CONSTRAINT ck_learning_episodes_episode_outcome_valid
                CHECK (outcome IN ('success','failure')),
            CONSTRAINT fk_learning_episodes_tunable_parameter_id_tunable_parameters
                FOREIGN KEY(tunable_parameter_id) REFERENCES tunable_parameters (id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_learning_episodes_param ON learning_episodes "
        "(tunable_parameter_id, created_at)"
    )

    op.execute(
        """
        CREATE TABLE heal_attempts (
            id VARCHAR(32) NOT NULL,
            product_id VARCHAR(32) NOT NULL,
            task_id VARCHAR(32),
            attempt_number INTEGER NOT NULL,
            healing_text TEXT NOT NULL,
            failed_categories JSONB NOT NULL,
            outcome VARCHAR(16) NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
            resolved_at TIMESTAMP WITH TIME ZONE,
            CONSTRAINT pk_heal_attempts PRIMARY KEY (id),
            CONSTRAINT ck_heal_attempts_heal_attempt_outcome_valid
                CHECK (outcome IN ('pending','healed','failed_again','escalated')),
            CONSTRAINT uq_heal_attempts_product_attempt UNIQUE (product_id, attempt_number),
            CONSTRAINT fk_heal_attempts_product_id_products
                FOREIGN KEY(product_id) REFERENCES products (id),
            CONSTRAINT fk_heal_attempts_task_id_tasks
                FOREIGN KEY(task_id) REFERENCES tasks (id)
        )
        """
    )

    op.execute("ALTER TABLE outreach_log ADD COLUMN converted_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE outreach_log ADD COLUMN ledger_entry_id VARCHAR(32)")
    op.execute(
        "ALTER TABLE outreach_log ADD CONSTRAINT fk_outreach_log_ledger_entry_id_ledger "
        "FOREIGN KEY (ledger_entry_id) REFERENCES ledger(id)"
    )
    op.execute(
        "ALTER TABLE outreach_log ADD CONSTRAINT uq_outreach_log_ledger_entry_id "
        "UNIQUE (ledger_entry_id)"
    )
    op.execute("CREATE INDEX ix_outreach_product_sent ON outreach_log (product_id, sent_at)")

    op.execute("ALTER TABLE llm_calls ADD COLUMN tunable_parameter_id VARCHAR(32)")
    op.execute(
        "ALTER TABLE llm_calls ADD CONSTRAINT fk_llm_calls_tunable_parameter_id_tunable_parameters "
        "FOREIGN KEY (tunable_parameter_id) REFERENCES tunable_parameters(id)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE llm_calls DROP CONSTRAINT IF EXISTS "
        "fk_llm_calls_tunable_parameter_id_tunable_parameters"
    )
    op.execute("ALTER TABLE llm_calls DROP COLUMN IF EXISTS tunable_parameter_id")

    op.execute("DROP INDEX IF EXISTS ix_outreach_product_sent")
    op.execute("ALTER TABLE outreach_log DROP CONSTRAINT IF EXISTS uq_outreach_log_ledger_entry_id")
    op.execute(
        "ALTER TABLE outreach_log DROP CONSTRAINT IF EXISTS fk_outreach_log_ledger_entry_id_ledger"
    )
    op.execute("ALTER TABLE outreach_log DROP COLUMN IF EXISTS ledger_entry_id")
    op.execute("ALTER TABLE outreach_log DROP COLUMN IF EXISTS converted_at")

    op.execute("DROP TABLE IF EXISTS heal_attempts CASCADE")
    op.execute("DROP TABLE IF EXISTS learning_episodes CASCADE")
    op.execute("DROP TABLE IF EXISTS tunable_parameters CASCADE")
