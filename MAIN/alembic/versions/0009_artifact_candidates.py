"""Persist immutable, source-bound artifact candidates without completion claims."""

from sqlalchemy import text

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE artifact_candidates (
        artifact_id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        contract_version integer NOT NULL CHECK (contract_version > 0),
        revision bigint NOT NULL CHECK (revision > 0),
        work_id uuid NOT NULL UNIQUE REFERENCES work_items(work_id),
        material_id uuid NOT NULL REFERENCES source_formal_materials(material_id),
        owner_id text NOT NULL CHECK (btrim(owner_id) <> ''),
        payload_hash char(64) NOT NULL,
        draft_hash char(64) NOT NULL,
        draft jsonb NOT NULL,
        markdown text NOT NULL CHECK (btrim(markdown) <> ''),
        state text NOT NULL CHECK (state='unreviewed_candidate'),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        FOREIGN KEY (task_id,contract_version) REFERENCES task_contracts(task_id,version)
    );
    CREATE INDEX artifact_candidates_current
        ON artifact_candidates(task_id,contract_version,owner_id);
    """)


def downgrade():
    count = op.get_bind().execute(text("SELECT count(*) FROM artifact_candidates")).scalar_one()
    if count:
        raise RuntimeError(f"Refusing to erase {count} artifact candidate(s)")
    op.execute("DROP TABLE artifact_candidates")
