"""Controlled formal source exit: dispatch admission, formal material, build successor.

Adds the ``build_artifact`` work kind, one-shot pre-dispatch admission records and
the versioned formal material store. Nothing here opens production entry: the
default worker cannot claim ``build_artifact`` until a handler is registered, and
dispatch admission refuses rather than inventing a browser session proof.

Downgrade refuses while any exit history exists so business rows are never
silently deleted or rewritten.
"""

from sqlalchemy import text

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _replace_single_column_check(table, column, allowed):
    """Swap the unnamed single-column check that an earlier migration created."""
    bind = op.get_bind()
    name = bind.execute(
        text(
            """
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY (con.conkey)
        WHERE con.conrelid = CAST(:table AS regclass)
          AND con.contype = 'c'
          AND array_length(con.conkey, 1) = 1
          AND att.attname = :column
        """
        ),
        {"table": table, "column": column},
    ).scalar_one()
    op.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{name}"')
    values = ", ".join(f"'{value}'" for value in allowed)
    op.execute(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" CHECK ({column} IN ({values}))')


def upgrade():
    # 1. The only new work kind. Claimed in the ``accepted`` contract state.
    _replace_single_column_check("work_items", "kind", ("form_goal", "read_source", "build_artifact"))

    # 2. Outcome evidence: a real error and its basis, never a bare conclusion.
    #    ``outcome`` already distinguishes succeeded/failed/outcome_unknown (0002).
    op.execute("ALTER TABLE source_reads ADD COLUMN outcome_basis text")
    op.execute("ALTER TABLE source_reads ADD COLUMN outcome_evidence text")
    op.execute("ALTER TABLE source_reads ADD COLUMN dispatch_generation bigint")
    op.execute("ALTER TABLE source_reads ALTER COLUMN dispatch_generation SET DEFAULT 0")
    op.execute("UPDATE source_reads SET dispatch_generation = 0 WHERE dispatch_generation IS NULL")
    op.execute("ALTER TABLE source_reads ALTER COLUMN dispatch_generation SET NOT NULL")
    # Evidence completeness and dispatch-generation monotonicity are enforced by
    # the controlled exit API (which refuses blank codes/evidence and is the only
    # writer of ``dispatch_generation``), not by a global CHECK: the long-standing
    # pre-dispatch retirement path legitimately writes ``failed`` without a basis
    # and must keep working unchanged.

    # 3. One-shot pre-dispatch admission. ``state='refused'`` rows are honest
    #    history that carry no send right; only ``granted`` is consumed on send.
    op.execute("""
    CREATE TABLE source_dispatch_admissions (
        admission_id uuid PRIMARY KEY,
        operation_id uuid NOT NULL REFERENCES source_reads(operation_id),
        attempt_id uuid NOT NULL REFERENCES source_operation_attempts(attempt_id),
        attempt_seq integer NOT NULL CHECK (attempt_seq > 0),
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        contract_version integer NOT NULL CHECK (contract_version > 0),
        revision bigint NOT NULL CHECK (revision > 0),
        work_id uuid NOT NULL REFERENCES work_items(work_id),
        lease_token uuid NOT NULL REFERENCES work_leases(token),
        action text NOT NULL CHECK (action IN ('video','subtitle')),
        bvid text NOT NULL CHECK (btrim(bvid) <> ''),
        page integer NOT NULL CHECK (page > 0),
        dispatch_generation bigint NOT NULL CHECK (dispatch_generation > 0),
        state text NOT NULL CHECK (state IN ('granted','consumed','released','refused')),
        refusal_code text,
        session_proof_kind text,
        session_proof jsonb,
        checked_connection_state text NOT NULL
            CHECK (checked_connection_state IN ('verified','unverified')),
        granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        consumed_at timestamptz,
        CHECK (
            (state = 'refused') = (refusal_code IS NOT NULL)
            AND (checked_connection_state = 'verified')
                = (session_proof_kind IS NOT NULL AND session_proof IS NOT NULL)
            AND (state IN ('granted','refused') OR consumed_at IS NOT NULL)
        ),
        FOREIGN KEY (task_id, contract_version) REFERENCES task_contracts(task_id, version)
    );
    -- At most one live dispatch admission per attempt: concurrent callers and
    -- resends can never produce a second send right. ``refused`` rows are honest
    -- history and may repeat, and a later attempt of the SAME operation may be
    -- admitted when recovery proves the prior attempt was never sent.
    CREATE UNIQUE INDEX source_dispatch_admissions_once_per_attempt
        ON source_dispatch_admissions (attempt_id)
        WHERE state IN ('granted','consumed');
    """)

    # 4. Formal material: hash plus time-pointed segments, formed once.
    op.execute("""
    CREATE TABLE source_formal_materials (
        material_id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES tasks(task_id),
        contract_version integer NOT NULL CHECK (contract_version > 0),
        revision bigint NOT NULL CHECK (revision > 0),
        work_id uuid NOT NULL REFERENCES work_items(work_id),
        operation_id uuid NOT NULL REFERENCES source_reads(operation_id),
        attempt_id uuid NOT NULL REFERENCES source_operation_attempts(attempt_id),
        permission_decision_id uuid NOT NULL
            REFERENCES source_permission_decisions(permission_decision_id),
        admission_id uuid NOT NULL REFERENCES source_dispatch_admissions(admission_id),
        owner_id text NOT NULL CHECK (btrim(owner_id) <> ''),
        action text NOT NULL CHECK (action IN ('video','subtitle')),
        bvid text NOT NULL CHECK (btrim(bvid) <> ''),
        page integer NOT NULL CHECK (page > 0),
        payload_hash char(64) NOT NULL CHECK (btrim(payload_hash) <> ''),
        adapter_version text NOT NULL CHECK (btrim(adapter_version) <> ''),
        parse_version text NOT NULL CHECK (btrim(parse_version) <> ''),
        segments jsonb NOT NULL,
        material_state text NOT NULL CHECK (material_state IN ('recorded')),
        successor_work_id uuid REFERENCES work_items(work_id),
        recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (operation_id),
        FOREIGN KEY (task_id, contract_version) REFERENCES task_contracts(task_id, version)
    );
    CREATE INDEX source_formal_materials_readable
        ON source_formal_materials (task_id, contract_version, owner_id, material_state);
    """)


def downgrade():
    bind = op.get_bind()
    history = bind.execute(
        text("""
        SELECT (SELECT count(*) FROM source_formal_materials)
             + (SELECT count(*) FROM source_dispatch_admissions)
        """)
    ).scalar_one()
    if history:
        raise RuntimeError(
            "Refusing to drop formal source exit history: "
            f"{history} row(s) would lose dispatch admission or material evidence; "
            "reconcile them first"
        )
    stranded = bind.execute(
        text("SELECT count(*) FROM work_items WHERE kind='build_artifact'")
    ).scalar_one()
    if stranded:
        raise RuntimeError(
            f"Refusing to drop build_artifact: {stranded} work row(s) would violate the "
            "previous constraint; finish or reconcile them first"
        )
    op.execute("DROP INDEX source_formal_materials_readable")
    op.execute("DROP TABLE source_formal_materials")
    op.execute("DROP INDEX source_dispatch_admissions_once_per_attempt")
    op.execute("DROP TABLE source_dispatch_admissions")
    op.execute("ALTER TABLE source_reads ALTER COLUMN dispatch_generation DROP NOT NULL")
    op.execute("ALTER TABLE source_reads DROP COLUMN dispatch_generation")
    op.execute("ALTER TABLE source_reads DROP COLUMN outcome_evidence")
    op.execute("ALTER TABLE source_reads DROP COLUMN outcome_basis")
    _replace_single_column_check("work_items", "kind", ("form_goal", "read_source"))
