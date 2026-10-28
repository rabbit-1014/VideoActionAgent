"""Internal goal-formation decisions, goals, blockers and safe recovery audits.

One immutable formation decision per contract version; lease receipts stay the
per-attempt record. `accepted`/`unformable` join the contract lifecycle states.
"""

from sqlalchemy import text

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _replace_state_check(states):
    """Swap the unnamed task_contracts.state check that 0001 created."""
    bind = op.get_bind()
    name = bind.execute(
        text("""
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
        WHERE con.conrelid = 'task_contracts'::regclass
          AND con.contype = 'c'
          AND array_length(con.conkey, 1) = 1
          AND att.attname = 'state'
        """)
    ).scalar_one()
    op.execute(f'ALTER TABLE task_contracts DROP CONSTRAINT "{name}"')
    allowed = ", ".join(f"'{state}'" for state in states)
    op.execute(f'ALTER TABLE task_contracts ADD CONSTRAINT "{name}" CHECK (state IN ({allowed}))')


def upgrade():
    _replace_state_check(("forming", "accepted", "unformable", "canceled", "superseded"))
    op.execute("""
    CREATE TABLE goal_formations (
        formation_id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        contract_version integer NOT NULL CHECK (contract_version > 0),
        work_id uuid NOT NULL REFERENCES work_items(work_id),
        lease_token uuid NOT NULL REFERENCES work_leases(token),
        revision bigint NOT NULL CHECK (revision > 0),
        decision text NOT NULL CHECK (decision IN ('accept','needs_user','unformable')),
        command jsonb NOT NULL CHECK ((command->>'schema_version')::integer = 1),
        command_hash char(64) NOT NULL,
        input_fingerprint char(64) NOT NULL,
        candidate_fingerprint char(64) NOT NULL,
        input_contract_review jsonb NOT NULL,
        policy_review jsonb NOT NULL,
        semantic_fidelity_proven boolean NOT NULL DEFAULT false
            CHECK (semantic_fidelity_proven = false),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (task_id, contract_version),
        FOREIGN KEY (task_id, contract_version) REFERENCES task_contracts(task_id, version)
    );

    CREATE TABLE goals (
        goal_id uuid PRIMARY KEY,
        task_id uuid NOT NULL,
        contract_version integer NOT NULL CHECK (contract_version > 0),
        required boolean NOT NULL,
        title text NOT NULL CHECK (btrim(title) <> ''),
        acceptance text NOT NULL CHECK (btrim(acceptance) <> ''),
        basis text NOT NULL CHECK (basis IN ('user_utterance','retractable_inference')),
        evidence text NOT NULL CHECK (btrim(evidence) <> ''),
        state text NOT NULL CHECK (state IN ('active','satisfied','failed','unsupported',
                                             'canceled','indeterminate','superseded')),
        progress text NOT NULL CHECK (progress IN ('none','partial','complete')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (task_id, contract_version) REFERENCES task_contracts(task_id, version),
        CHECK ((state = 'satisfied') = (progress = 'complete'))
    );
    CREATE INDEX goals_by_version ON goals (task_id, contract_version, required);

    CREATE TABLE goal_blockers (
        blocker_id uuid PRIMARY KEY,
        task_id uuid NOT NULL,
        contract_version integer NOT NULL CHECK (contract_version > 0),
        question text NOT NULL CHECK (btrim(question) <> ''),
        waiting_condition text NOT NULL CHECK (btrim(waiting_condition) <> ''),
        responsible_party text NOT NULL CHECK (btrim(responsible_party) <> ''),
        advanceable boolean NOT NULL,
        exit_path text NOT NULL CHECK (btrim(exit_path) <> ''),
        state text NOT NULL CHECK (state IN ('open','resolved','expired','canceled')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (task_id, contract_version) REFERENCES task_contracts(task_id, version)
    );
    CREATE INDEX goal_blockers_by_version ON goal_blockers (task_id, contract_version, state);

    CREATE TABLE form_goal_recoveries (
        recovery_id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        contract_version integer NOT NULL CHECK (contract_version > 0),
        revision bigint NOT NULL CHECK (revision > 0),
        work_id uuid NOT NULL REFERENCES work_items(work_id),
        lease_token uuid NOT NULL REFERENCES work_leases(token),
        action text NOT NULL CHECK (action IN ('reset_to_ready','already_released',
            'decision_already_recorded','inconsistent_decision_state',
            'receipt_already_recorded','requires_source_reconciliation',
            'version_no_longer_current')),
        detail jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE INDEX form_goal_recoveries_by_work ON form_goal_recoveries (work_id, created_at);
    """)


def downgrade():
    op.execute("""
    DROP TABLE form_goal_recoveries;
    DROP TABLE goal_blockers;
    DROP TABLE goals;
    DROP TABLE goal_formations;
    """)
    _replace_state_check(("forming", "canceled", "superseded"))
