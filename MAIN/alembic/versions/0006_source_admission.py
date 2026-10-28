"""Formal pre-dispatch source admission: Proposal -> PermissionDecision -> prepared Operation.

One formal source action per work item. The admission commits three ordered
transition commits on the existing `transitions` sequence (N+1 proposed, N+2
authorized, N+3 consumed) and creates the prepared Operation under the
preallocated operation id at the consume step. Nothing here dispatches, and the
permission decision records the browser connection state as unverified.
"""

from sqlalchemy import text

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

POLICY_VERSION = "accepted-local-bilibili-read-v1"


def upgrade():
    op.execute(f"""
    CREATE TABLE source_proposals (
        proposal_id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        contract_version integer NOT NULL CHECK (contract_version > 0),
        revision bigint NOT NULL CHECK (revision > 0),
        work_id uuid NOT NULL REFERENCES work_items(work_id),
        lease_token uuid NOT NULL REFERENCES work_leases(token),
        operation_id uuid NOT NULL UNIQUE,
        state text NOT NULL CHECK (state IN
            ('proposed','authorized','rejected','consumed','expired','superseded')),
        action text NOT NULL CHECK (action IN ('video','subtitle')),
        bvid text NOT NULL CHECK (btrim(bvid) <> ''),
        page integer NOT NULL CHECK (page > 0),
        effect text NOT NULL CHECK (effect = 'read_only'),
        capability_version text NOT NULL CHECK (btrim(capability_version) <> ''),
        policy_version text NOT NULL CHECK (policy_version = '{POLICY_VERSION}'),
        purpose text NOT NULL CHECK (btrim(purpose) <> ''),
        stop_condition text NOT NULL CHECK (btrim(stop_condition) <> ''),
        time_limit text NOT NULL CHECK (btrim(time_limit) <> ''),
        goal_ref jsonb NOT NULL,
        input_fingerprint char(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (work_id),
        UNIQUE (proposal_id, operation_id),
        FOREIGN KEY (task_id, contract_version) REFERENCES task_contracts(task_id, version)
    );

    CREATE TABLE source_permission_decisions (
        permission_decision_id uuid PRIMARY KEY,
        proposal_id uuid NOT NULL REFERENCES source_proposals(proposal_id),
        operation_id uuid NOT NULL,
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        contract_version integer NOT NULL CHECK (contract_version > 0),
        revision bigint NOT NULL CHECK (revision > 0),
        owner_id text NOT NULL CHECK (btrim(owner_id) <> ''),
        authorization_basis text NOT NULL
            CHECK (authorization_basis = 'contract_local_browser_actions'),
        entry_scope jsonb NOT NULL,
        action text NOT NULL CHECK (action IN ('video','subtitle')),
        bvid text NOT NULL CHECK (btrim(bvid) <> ''),
        page integer NOT NULL CHECK (page > 0),
        effect text NOT NULL CHECK (effect = 'read_only'),
        data_purpose text NOT NULL CHECK (btrim(data_purpose) <> ''),
        policy_version text NOT NULL CHECK (policy_version = '{POLICY_VERSION}'),
        decision text NOT NULL CHECK (decision IN ('auto_allow','require_confirmation','reject')),
        connection_state text NOT NULL CHECK (connection_state = 'unverified'),
        reason_codes jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (proposal_id),
        UNIQUE (permission_decision_id, operation_id),
        UNIQUE (permission_decision_id, operation_id, proposal_id),
        FOREIGN KEY (proposal_id, operation_id)
            REFERENCES source_proposals(proposal_id, operation_id),
        FOREIGN KEY (task_id, contract_version) REFERENCES task_contracts(task_id, version)
    );

    ALTER TABLE source_reads ADD COLUMN permission_decision_id uuid;
    ALTER TABLE source_reads ADD CONSTRAINT source_reads_formal_permission_identity
        CHECK (permission_decision_id IS NULL OR permission_id = permission_decision_id);
    ALTER TABLE source_reads ADD CONSTRAINT source_reads_formal_decision_causality
        FOREIGN KEY (permission_decision_id, operation_id, proposal_id)
        REFERENCES source_permission_decisions
            (permission_decision_id, operation_id, proposal_id);
    ALTER TABLE source_reads ADD CONSTRAINT source_reads_formal_decision_unique
        UNIQUE (permission_decision_id);

    ALTER TABLE transitions ADD COLUMN object_kind text;
    ALTER TABLE transitions ADD COLUMN object_ref uuid;
    ALTER TABLE transitions ADD COLUMN from_state text;
    ALTER TABLE transitions ADD COLUMN to_state text;
    ALTER TABLE transitions ADD COLUMN reason_ref uuid;
    ALTER TABLE transitions ADD COLUMN policy_version text;
    ALTER TABLE transitions ADD COLUMN dedup_key text;
    CREATE UNIQUE INDEX transitions_dedup
        ON transitions (task_id, dedup_key) WHERE dedup_key IS NOT NULL;
    """)


def downgrade():
    history = op.get_bind().execute(
        text("""
        SELECT (SELECT count(*) FROM source_proposals)
             + (SELECT count(*) FROM source_permission_decisions)
             + (SELECT count(*) FROM source_reads WHERE permission_decision_id IS NOT NULL)
        """)
    ).scalar_one()
    if history:
        raise RuntimeError(
            "Refusing to drop formal source admission history: reconcile the admitted "
            "proposals, permission decisions and prepared operations first"
        )
    op.execute("""
    DROP INDEX transitions_dedup;
    ALTER TABLE transitions DROP COLUMN dedup_key;
    ALTER TABLE transitions DROP COLUMN policy_version;
    ALTER TABLE transitions DROP COLUMN reason_ref;
    ALTER TABLE transitions DROP COLUMN to_state;
    ALTER TABLE transitions DROP COLUMN from_state;
    ALTER TABLE transitions DROP COLUMN object_ref;
    ALTER TABLE transitions DROP COLUMN object_kind;
    ALTER TABLE source_reads DROP CONSTRAINT source_reads_formal_decision_unique;
    ALTER TABLE source_reads DROP CONSTRAINT source_reads_formal_decision_causality;
    ALTER TABLE source_reads DROP CONSTRAINT source_reads_formal_permission_identity;
    ALTER TABLE source_reads DROP COLUMN permission_decision_id;
    DROP TABLE source_permission_decisions;
    DROP TABLE source_proposals;
    """)
