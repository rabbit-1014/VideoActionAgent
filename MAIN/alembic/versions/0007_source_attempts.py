"""Formal source operation attempts: same-identity recovery of prepared operations.

Attempt history per Operation with at most one current prepared attempt. The
upgrade only seeds attempt 1 for formal rows that still prove they were never
dispatched; anything else becomes an unknown pre-upgrade attempt instead of an
invented "undispatched" history. Downgrade refuses while attempt history exists.
"""

from sqlalchemy import text

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

PROVABLY_UNDISPATCHED = (
    "r.phase = 'prepared' AND r.outcome = 'none'"
    " AND r.dispatched_at IS NULL AND r.received_at IS NULL"
    " AND r.raw_payload IS NULL AND r.material IS NULL AND r.payload_hash IS NULL"
)


def upgrade():
    op.execute(f"""
    CREATE TABLE source_operation_attempts (
        attempt_id uuid PRIMARY KEY,
        operation_id uuid NOT NULL REFERENCES source_reads(operation_id),
        attempt_seq integer NOT NULL CHECK (attempt_seq > 0),
        work_id uuid NOT NULL REFERENCES work_items(work_id),
        lease_token uuid NOT NULL REFERENCES work_leases(token),
        state text NOT NULL CHECK (state IN ('prepared','closed')),
        dispatch_admission_obtained boolean,
        close_reason text,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        closed_at timestamptz,
        UNIQUE (operation_id, attempt_seq),
        CHECK (state = 'prepared' OR close_reason IS NOT NULL)
    );
    CREATE UNIQUE INDEX source_operation_attempts_current
        ON source_operation_attempts (operation_id) WHERE state = 'prepared';

    INSERT INTO source_operation_attempts
    (attempt_id,operation_id,attempt_seq,work_id,lease_token,state,
     dispatch_admission_obtained,close_reason,created_at,closed_at)
    SELECT r.attempt_id, r.operation_id, 1, r.work_id, r.lease_token,
           CASE WHEN {PROVABLY_UNDISPATCHED} THEN 'prepared' ELSE 'closed' END,
           CASE WHEN {PROVABLY_UNDISPATCHED} THEN false ELSE NULL END,
           CASE WHEN {PROVABLY_UNDISPATCHED}
                THEN NULL ELSE 'pre_upgrade_state_not_provably_undispatched' END,
           COALESCE(r.created_at, clock_timestamp()),
           CASE WHEN {PROVABLY_UNDISPATCHED} THEN NULL ELSE clock_timestamp() END
    FROM source_reads r
    WHERE r.permission_decision_id IS NOT NULL;
    """)


def downgrade():
    history = op.get_bind().execute(
        text("SELECT count(*) FROM source_operation_attempts")
    ).scalar_one()
    if history:
        raise RuntimeError(
            f"Refusing to drop source attempt history: {history} attempt row(s) would be "
            "lost; reconcile the formal source operations first"
        )
    op.execute("""
    DROP INDEX source_operation_attempts_current;
    DROP TABLE source_operation_attempts;
    """)
