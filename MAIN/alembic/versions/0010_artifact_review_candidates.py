"""Store append-only, source-bound advisory reviews of artifact candidates."""

from sqlalchemy import text

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE artifact_review_candidates (
        review_id uuid PRIMARY KEY,
        artifact_id uuid NOT NULL REFERENCES artifact_candidates(artifact_id),
        draft_hash char(64) NOT NULL,
        payload_hash char(64) NOT NULL,
        reviewer_model_id text NOT NULL CHECK (btrim(reviewer_model_id) <> ''),
        review_hash char(64) NOT NULL,
        review jsonb NOT NULL CHECK (
            jsonb_typeof(review)='object'
            AND review->>'kind'='quality_review_candidate'
            AND review->>'advisory_only'='true'
            AND review->>'draft_sha256'=draft_hash
            AND review->>'payload_hash'=payload_hash
            AND review->>'reviewer_model_id'=reviewer_model_id
            AND NOT (review ? 'quality_passed')
            AND NOT (review ? 'completed')
            AND NOT (review ? 'credential')
        ),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (artifact_id,review_hash)
    );
    CREATE INDEX artifact_review_candidates_by_artifact
        ON artifact_review_candidates(artifact_id,created_at,review_id);
    """)


def downgrade():
    count = op.get_bind().execute(
        text("SELECT count(*) FROM artifact_review_candidates")
    ).scalar_one()
    if count:
        raise RuntimeError(f"Refusing to erase {count} advisory review(s)")
    op.execute("DROP TABLE artifact_review_candidates")
