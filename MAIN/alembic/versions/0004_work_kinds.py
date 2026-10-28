"""Register the read_source work kind beside form_goal.

One work item per (task, contract version, kind) is unchanged; only the kind
registry grows. Downgrade refuses while read_source rows exist so business
records are never silently deleted or rewritten.
"""

from sqlalchemy import text

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def _replace_kind_check(kinds):
    """Swap the unnamed work_items.kind check that 0001 created."""
    bind = op.get_bind()
    name = bind.execute(
        text("""
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
        WHERE con.conrelid = 'work_items'::regclass
          AND con.contype = 'c'
          AND array_length(con.conkey, 1) = 1
          AND att.attname = 'kind'
        """)
    ).scalar_one()
    op.execute(f'ALTER TABLE work_items DROP CONSTRAINT "{name}"')
    allowed = ", ".join(f"'{kind}'" for kind in kinds)
    op.execute(f'ALTER TABLE work_items ADD CONSTRAINT "{name}" CHECK (kind IN ({allowed}))')


def upgrade():
    _replace_kind_check(("form_goal", "read_source"))


def downgrade():
    bind = op.get_bind()
    stranded = bind.execute(
        text("SELECT count(*) FROM work_items WHERE kind='read_source'")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"Refusing to drop read_source: {stranded} work row(s) would violate the "
            "previous constraint; finish or reconcile them first"
        )
    _replace_kind_check(("form_goal",))
