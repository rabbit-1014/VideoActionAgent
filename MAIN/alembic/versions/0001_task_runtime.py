"""Initial task runtime; no goal or product completion implementation."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE tasks (
        task_id uuid PRIMARY KEY,
        owner_id text NOT NULL,
        idempotency_key text NOT NULL,
        request_hash char(64) NOT NULL,
        current_version integer NOT NULL CHECK (current_version > 0),
        revision bigint NOT NULL CHECK (revision > 0),
        run_id uuid NOT NULL,
        trace_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (owner_id, idempotency_key)
    );
    CREATE TABLE task_contracts (
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        version integer NOT NULL CHECK (version > 0),
        state text NOT NULL CHECK (state IN ('forming','canceled','superseded')),
        input jsonb NOT NULL CHECK ((input->>'schema_version')::integer = 1),
        PRIMARY KEY (task_id, version)
    );
    CREATE TABLE transitions (
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        revision bigint NOT NULL CHECK (revision > 0),
        kind text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (task_id, revision)
    );
    CREATE TABLE work_items (
        work_id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        contract_version integer NOT NULL,
        expected_revision bigint NOT NULL,
        run_id uuid NOT NULL,
        kind text NOT NULL CHECK (kind = 'form_goal'),
        state text NOT NULL CHECK (state IN ('ready','leased','closed','suppressed')),
        available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        lease_token uuid,
        lease_owner text,
        lease_until timestamptz,
        receipt_id uuid,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (task_id, contract_version, kind),
        FOREIGN KEY (task_id, contract_version) REFERENCES task_contracts(task_id, version),
        CHECK (state != 'leased' OR
               (lease_token IS NOT NULL AND lease_owner IS NOT NULL AND lease_until IS NOT NULL))
    );
    CREATE INDEX work_ready ON work_items (available_at, work_id) WHERE state = 'ready';
    CREATE INDEX work_expired ON work_items (lease_until) WHERE state = 'leased';
    CREATE TABLE work_leases (
        token uuid PRIMARY KEY,
        work_id uuid NOT NULL REFERENCES work_items(work_id),
        owner text NOT NULL,
        revision bigint NOT NULL,
        mode text NOT NULL CHECK (mode IN ('execute','reconcile')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE TABLE work_receipts (
        receipt_id uuid PRIMARY KEY,
        lease_token uuid NOT NULL UNIQUE REFERENCES work_leases(token),
        work_id uuid NOT NULL REFERENCES work_items(work_id),
        disposition text NOT NULL CHECK (disposition IN ('recorded','stale')),
        outcome text NOT NULL CHECK (outcome IN ('ok','error')),
        payload jsonb NOT NULL CHECK ((payload->>'schema_version')::integer = 1),
        payload_hash char(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    ALTER TABLE work_items ADD CONSTRAINT receipt_ref
        FOREIGN KEY (receipt_id) REFERENCES work_receipts(receipt_id);
    """)


def downgrade():
    op.execute("""
    ALTER TABLE work_items DROP CONSTRAINT receipt_ref;
    DROP TABLE work_receipts;
    DROP TABLE work_leases;
    DROP TABLE work_items;
    DROP TABLE transitions;
    DROP TABLE task_contracts;
    DROP TABLE tasks;
    """)
