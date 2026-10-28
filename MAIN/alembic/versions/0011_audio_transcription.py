"""Explicit independently authorized local audio transcription material."""

from sqlalchemy import text

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

ACTION_TABLES = ("source_reads", "source_proposals", "source_permission_decisions",
                 "source_dispatch_admissions", "source_formal_materials")


def replace_check(table, column, allowed):
    name = op.get_bind().execute(text("""
        SELECT con.conname FROM pg_constraint con
        JOIN pg_attribute att ON att.attrelid=con.conrelid AND att.attnum=ANY(con.conkey)
        WHERE con.conrelid=CAST(:table AS regclass) AND con.contype='c'
          AND array_length(con.conkey,1)=1 AND att.attname=:column
    """), {"table": table, "column": column}).scalar_one()
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
    values = ",".join(f"'{value}'" for value in allowed)
    op.execute(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" CHECK ({column} IN ({values}))')


def upgrade():
    for table in ACTION_TABLES:
        replace_check(table, "action", ("video", "subtitle", "audio_transcription"))
    for table in ("source_proposals", "source_permission_decisions"):
        replace_check(table, "policy_version", ("accepted-local-bilibili-read-v1",
                                               "accepted-local-bilibili-audio-asr-v1"))
    replace_check("source_permission_decisions", "authorization_basis",
                  ("contract_local_browser_actions", "contract_local_source_actions"))
    op.execute("ALTER TABLE source_formal_materials ADD COLUMN provenance jsonb NOT NULL DEFAULT '{}'::jsonb")
    op.execute("""
        ALTER TABLE source_formal_materials ADD CONSTRAINT source_asr_provenance CHECK (
          action <> 'audio_transcription' OR (
            jsonb_typeof(provenance)='object'
            AND provenance ?& ARRAY['kind','cid','audio_sha256','asr_input_sha256','models',
              'source_evidence','timestamp_kind','automatically_verified_truth']
            AND provenance->>'kind'='audio_transcription'
            AND provenance->>'timestamp_kind'='vad_segment'
            AND provenance->'automatically_verified_truth'='false'::jsonb) IS TRUE)
    """)


def downgrade():
    count = sum(op.get_bind().execute(text(f"SELECT count(*) FROM {table} WHERE action='audio_transcription'"))
                .scalar_one() for table in ACTION_TABLES)
    count += op.get_bind().execute(text("SELECT count(*) FROM source_formal_materials WHERE provenance <> '{}'::jsonb")).scalar_one()
    if count:
        raise RuntimeError("Refusing to discard audio transcription history")
    op.execute("ALTER TABLE source_formal_materials DROP CONSTRAINT source_asr_provenance")
    op.execute("ALTER TABLE source_formal_materials DROP COLUMN provenance")
    for table in ACTION_TABLES:
        replace_check(table, "action", ("video", "subtitle"))
    for table in ("source_proposals", "source_permission_decisions"):
        replace_check(table, "policy_version", ("accepted-local-bilibili-read-v1",))
    replace_check("source_permission_decisions", "authorization_basis", ("contract_local_browser_actions",))
