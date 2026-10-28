"""Retire provably undispatched source operations so recovery can proceed.

A retired row is kept as failed pre-dispatch history and may never be rewritten
as success. The (work_id, action) business key now only constrains rows that are
not retired; a fresh execute lease registers a new operation identity. Downgrade
refuses while duplicate action history would be lost.
"""

from sqlalchemy import text

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

RETIRE_SHAPE = """
    ALTER TABLE source_reads ADD CONSTRAINT source_reads_retired_shape CHECK (
        retired_pre_dispatch = false OR (
            phase = 'closed' AND outcome = 'failed'
            AND COALESCE(disposition, '') = 'stale'
            AND COALESCE(error_code, '') = 'interrupted_before_dispatch'
            AND dispatched_at IS NULL AND received_at IS NULL
            AND raw_payload IS NULL AND material IS NULL AND payload_hash IS NULL))
"""

ACTION_UNIQUE_LOOKUP = """
    SELECT con.conname
    FROM pg_constraint con
    WHERE con.conrelid = 'source_reads'::regclass
      AND con.contype = 'u'
      AND (SELECT array_agg(att.attname::text ORDER BY att.attname)
           FROM pg_attribute att
           WHERE att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey))
          = ARRAY['action','work_id']::text[]
"""


def upgrade():
    op.execute("ALTER TABLE source_reads ADD COLUMN retired_pre_dispatch boolean NOT NULL DEFAULT false")
    op.execute(RETIRE_SHAPE)
    name = op.get_bind().execute(text(ACTION_UNIQUE_LOOKUP)).scalar_one()
    op.execute(f'ALTER TABLE source_reads DROP CONSTRAINT "{name}"')
    op.execute("""
    CREATE UNIQUE INDEX source_reads_active_action
        ON source_reads (work_id, action) WHERE NOT retired_pre_dispatch
    """)


def downgrade():
    duplicates = op.get_bind().execute(
        text("""
        SELECT count(*) FROM (
            SELECT work_id, action FROM source_reads
            GROUP BY work_id, action HAVING count(*) > 1) duplicates
        """)
    ).scalar_one()
    if duplicates:
        raise RuntimeError(
            f"Refusing to drop retired_pre_dispatch: {duplicates} (work_id, action) group(s) "
            "have duplicate history that the old constraint cannot keep"
        )
    retired = op.get_bind().execute(
        text("SELECT count(*) FROM source_reads WHERE retired_pre_dispatch")
    ).scalar_one()
    if retired:
        raise RuntimeError(
            f"Refusing to drop retired_pre_dispatch: {retired} retired operation(s) "
            "would lose their pre-dispatch recovery meaning"
        )
    op.execute("DROP INDEX source_reads_active_action")
    op.execute("ALTER TABLE source_reads DROP CONSTRAINT source_reads_retired_shape")
    op.execute("ALTER TABLE source_reads DROP COLUMN retired_pre_dispatch")
    op.execute("ALTER TABLE source_reads ADD CONSTRAINT source_reads_work_id_action_key UNIQUE (work_id, action)")
