"""One local read-only source operation path, not a generic tool registry."""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE source_reads (
        operation_id uuid PRIMARY KEY,
        proposal_id uuid NOT NULL UNIQUE,
        permission_id uuid NOT NULL UNIQUE,
        attempt_id uuid NOT NULL UNIQUE,
        admission_id uuid NOT NULL UNIQUE,
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        contract_version integer NOT NULL,
        revision bigint NOT NULL,
        work_id uuid NOT NULL REFERENCES work_items(work_id),
        lease_token uuid NOT NULL REFERENCES work_leases(token),
        action text NOT NULL CHECK (action IN ('video','subtitle')),
        adapter_version text NOT NULL,
        proposal jsonb NOT NULL,
        permission jsonb NOT NULL,
        phase text NOT NULL CHECK (phase IN ('prepared','dispatching','closed')),
        outcome text NOT NULL CHECK (outcome IN ('none','succeeded','failed','outcome_unknown')),
        disposition text CHECK (disposition IN ('recorded','stale')),
        error_code text,
        raw_payload bytea,
        payload_hash char(64),
        material jsonb,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        dispatched_at timestamptz,
        received_at timestamptz,
        UNIQUE (work_id, action),
        FOREIGN KEY (task_id,contract_version) REFERENCES task_contracts(task_id,version),
        CHECK ((phase='closed') = (outcome!='none'))
    );
    """)


def downgrade():
    op.execute("DROP TABLE source_reads")
