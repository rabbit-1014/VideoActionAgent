import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from test_runtime import accepted_read_source_lease, build_command, insert_work, submit

from alembic import command


def test_committed_claim_survives_abrupt_worker_exit(runtime, engine):
    task = submit(runtime)
    with engine.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar_one()
    env = {**os.environ, "PROBE_SCHEMA": schema}
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("worker_crash_probe.py"))],
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 23, result.stderr.decode(errors="replace")
    assert runtime.get("alice", task["task_id"])["work_state"] == "leased"
    assert runtime.claim("replacement", {"form_goal"}) is None
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    recovery = runtime.claim("replacement", {"form_goal"}, recover_expired=True)
    assert recovery.mode == "reconcile"
    assert runtime.get("alice", task["task_id"])["completed"] is False


def test_migration_downgrade_upgrade_roundtrip(engine):
    cfg = Config("alembic.ini")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
        assert conn.execute(text("SELECT to_regclass('tasks')")).scalar_one() is None
        command.upgrade(cfg, "head")
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar_one() == 0


def test_work_kind_registration_accepts_successor_and_rejects_unknown_kind(runtime, engine):
    from sqlalchemy.exc import IntegrityError

    task = submit(runtime)
    insert_work(engine, task, "read_source", 1)
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text("""
            INSERT INTO work_items
            (work_id,task_id,contract_version,expected_revision,run_id,kind,state)
            VALUES (:work,:task,:version,1,:run,'unknown_kind','ready')
            """),
            {
                "work": uuid4(),
                "task": task["task_id"],
                "version": task["current_version"],
                "run": uuid4(),
            },
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT kind FROM work_items ORDER BY kind")).scalars().all() == [
            "form_goal",
            "read_source",
        ]


def test_migration_downgrade_refuses_when_read_source_rows_exist(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim))
    formed = runtime.get("alice", task["task_id"])
    insert_work(engine, task, "read_source", formed["revision"])
    cfg = Config("alembic.ini")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="read_source"):
            command.downgrade(cfg, "0003")
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM work_items WHERE kind='read_source'")
            ).scalar_one()
            == 1
        )
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar_one() == 1


def insert_source_row(engine, task, claim, *, retired, outcome="none", phase="prepared"):
    from video_action_agent.source_access import ADAPTER_VERSION

    operation = uuid4()
    retired_shape = {
        "phase": "closed",
        "outcome": "failed",
        "disposition": "stale",
        "error_code": "interrupted_before_dispatch",
    }
    if retired:
        phase = retired_shape["phase"] if phase == "prepared" else phase
        outcome = retired_shape["outcome"] if outcome == "none" else outcome
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO source_reads
            (operation_id,proposal_id,permission_id,attempt_id,admission_id,task_id,
             contract_version,revision,work_id,lease_token,action,adapter_version,
             proposal,permission,phase,outcome,disposition,error_code,retired_pre_dispatch)
            VALUES (:op,:proposal_id,:permission_id,:attempt,:admission,:task,:version,
                    :revision,:work,:token,'video',:adapter,CAST(:proposal AS jsonb),
                    CAST(:permission AS jsonb),:phase,:outcome,:disposition,:error_code,:retired)
            """),
            {
                "op": operation,
                "proposal_id": uuid4(),
                "permission_id": uuid4(),
                "attempt": uuid4(),
                "admission": uuid4(),
                "task": task["task_id"],
                "version": claim.contract_version,
                "revision": claim.revision,
                "work": claim.work_id,
                "token": claim.token,
                "adapter": ADAPTER_VERSION,
                "proposal": "{}",
                "permission": "{}",
                "phase": phase,
                "outcome": outcome,
                "disposition": retired_shape["disposition"] if retired else None,
                "error_code": retired_shape["error_code"] if retired else None,
                "retired": retired,
            },
        )
    return operation


def test_retired_history_may_repeat_an_action_but_active_rows_stay_unique(runtime, engine):
    from sqlalchemy.exc import IntegrityError

    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    insert_source_row(engine, task, claim, retired=True)
    insert_source_row(engine, task, claim, retired=True)
    insert_source_row(engine, task, claim, retired=False)
    with pytest.raises(IntegrityError):
        insert_source_row(engine, task, claim, retired=False)
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_reads WHERE retired_pre_dispatch")
            ).scalar_one()
            == 2
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_reads WHERE NOT retired_pre_dispatch")
            ).scalar_one()
            == 1
        )


def test_retired_rows_are_forced_to_the_failed_history_shape(runtime, engine):
    from sqlalchemy.exc import IntegrityError

    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    with pytest.raises(IntegrityError):
        insert_source_row(engine, task, claim, retired=True, outcome="succeeded")


@pytest.mark.parametrize("timestamp_column", ["dispatched_at", "received_at"])
def test_retired_pre_dispatch_history_cannot_have_external_timestamps(
    runtime, engine, timestamp_column
):
    from sqlalchemy.exc import IntegrityError

    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    operation = insert_source_row(engine, task, claim, retired=True)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE source_reads SET {timestamp_column}=clock_timestamp() WHERE operation_id=:op"
            ),
            {"op": operation},
        )
    with engine.connect() as conn:
        assert (
            conn.execute(
                text(f"SELECT {timestamp_column} FROM source_reads WHERE operation_id=:op"),
                {"op": operation},
            ).scalar_one()
            is None
        )


@pytest.mark.parametrize("required_column", ["disposition", "error_code"])
def test_retired_pre_dispatch_history_requires_non_null_terminal_reason(
    runtime, engine, required_column
):
    from sqlalchemy.exc import IntegrityError

    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    operation = insert_source_row(engine, task, claim, retired=True)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            text(f"UPDATE source_reads SET {required_column}=NULL WHERE operation_id=:op"),
            {"op": operation},
        )


def test_migration_downgrade_refuses_even_one_retired_operation(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    operation = insert_source_row(engine, task, claim, retired=True)
    cfg = Config("alembic.ini")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="retired"):
            command.downgrade(cfg, "0004")
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT retired_pre_dispatch FROM source_reads WHERE operation_id=:op"),
                {"op": operation},
            ).scalar_one()
            is True
        )


def test_migration_downgrade_refuses_when_duplicate_action_history_exists(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    insert_source_row(engine, task, claim, retired=True)
    insert_source_row(engine, task, claim, retired=False)
    cfg = Config("alembic.ini")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="duplicate"):
            command.downgrade(cfg, "0004")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_reads")).scalar_one() == 2


def test_migration_0006_downgrade_refuses_when_formal_admission_history_exists(runtime, engine):
    _task, lease = accepted_read_source_lease(runtime, engine, key="downgrade")
    admission = runtime.prepare_source_read(lease, "alice", "subtitle")
    assert admission.uniqueness_decision == "new"
    cfg = Config("alembic.ini")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="formal"):
            command.downgrade(cfg, "0005")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_proposals")).scalar_one() == 1
        assert (
            conn.execute(text("SELECT count(*) FROM source_permission_decisions")).scalar_one() == 1
        )


def insert_formal_chain(engine, task, claim, *, dispatched):
    """Rows as they looked before 0007: formal admission without attempt history."""
    from video_action_agent.source_access import ADAPTER_VERSION

    proposal_id, decision_id, operation_id, attempt_id = uuid4(), uuid4(), uuid4(), uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO source_proposals
            (proposal_id,task_id,contract_version,revision,work_id,lease_token,operation_id,
             state,action,bvid,page,effect,capability_version,policy_version,purpose,
             stop_condition,time_limit,goal_ref,input_fingerprint)
            VALUES (:proposal,:task,1,:revision,:work,:token,:operation,'consumed','subtitle',
                    'BV1gD421u75J',1,'read_only',:capability,'accepted-local-bilibili-read-v1',
                    'purpose','stop','limit',CAST(:goal_ref AS jsonb),:input_fp)
            """),
            {
                "proposal": proposal_id,
                "task": task["task_id"],
                "revision": claim.revision,
                "work": claim.work_id,
                "token": claim.token,
                "operation": operation_id,
                "capability": ADAPTER_VERSION,
                "goal_ref": '{"required_goal_ids":[]}',
                "input_fp": "0" * 64,
            },
        )
        conn.execute(
            text("""
            INSERT INTO source_permission_decisions
            (permission_decision_id,proposal_id,operation_id,task_id,contract_version,revision,
             owner_id,authorization_basis,entry_scope,action,bvid,page,effect,data_purpose,
             policy_version,decision,connection_state,reason_codes)
            VALUES (:decision,:proposal,:operation,:task,1,:revision,'alice',
                    'contract_local_browser_actions',CAST(:scope AS jsonb),'subtitle',
                    'BV1gD421u75J',1,'read_only','purpose',
                    'accepted-local-bilibili-read-v1','auto_allow','unverified',CAST(:reasons AS jsonb))
            """),
            {
                "decision": decision_id,
                "proposal": proposal_id,
                "operation": operation_id,
                "task": task["task_id"],
                "revision": claim.revision,
                "scope": '["subtitle"]',
                "reasons": "[]",
            },
        )
        conn.execute(
            text("""
            INSERT INTO source_reads
            (operation_id,proposal_id,permission_id,attempt_id,admission_id,task_id,
             contract_version,revision,work_id,lease_token,action,adapter_version,
             proposal,permission,phase,outcome,permission_decision_id,dispatched_at)
            VALUES (:operation,:proposal,:decision,:attempt,:admission,:task,1,:revision,
                    :work,:token,'subtitle',:adapter,CAST(:proposal_doc AS jsonb),
                    CAST(:permission_doc AS jsonb),'prepared','none',:decision,
                    CASE WHEN :dispatched THEN clock_timestamp() ELSE NULL END)
            """),
            {
                "operation": operation_id,
                "proposal": proposal_id,
                "decision": decision_id,
                "attempt": attempt_id,
                "admission": uuid4(),
                "task": task["task_id"],
                "revision": claim.revision,
                "work": claim.work_id,
                "token": claim.token,
                "adapter": ADAPTER_VERSION,
                "proposal_doc": "{}",
                "permission_doc": "{}",
                "dispatched": dispatched,
            },
        )
    return operation_id, attempt_id


def test_migration_0007_seeds_attempts_without_fabricating_undispatched_history(runtime, engine):
    cfg = Config("alembic.ini")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0006")
    clean_task, clean_lease = accepted_read_source_lease(runtime, engine, key="seed-clean")
    clean_operation, clean_attempt = insert_formal_chain(
        engine, clean_task, clean_lease, dispatched=False
    )
    busy_task, busy_lease = accepted_read_source_lease(runtime, engine, key="seed-busy")
    busy_operation, busy_attempt = insert_formal_chain(
        engine, busy_task, busy_lease, dispatched=True
    )
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        seeded = conn.execute(
            text("SELECT operation_id,attempt_id,attempt_seq,state,"
                 "dispatch_admission_obtained,close_reason FROM source_operation_attempts"
                 " ORDER BY created_at,attempt_seq")
        ).mappings().all()
    by_operation = {row["operation_id"]: row for row in seeded}
    clean = by_operation[clean_operation]
    assert clean["attempt_id"] == clean_attempt
    assert (clean["attempt_seq"], clean["state"], clean["dispatch_admission_obtained"]) == (
        1,
        "prepared",
        False,
    )
    assert clean["close_reason"] is None
    busy = by_operation[busy_operation]
    assert busy["attempt_id"] == busy_attempt
    assert (busy["attempt_seq"], busy["state"]) == (1, "closed")
    assert busy["dispatch_admission_obtained"] is None
    assert busy["close_reason"] == "pre_upgrade_state_not_provably_undispatched"
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="attempt"):
            command.downgrade(cfg, "0006")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_operation_attempts")).scalar_one() == 2


def test_real_http_process_admission_and_polling(engine):
    import socket
    import time

    import httpx
    from test_runtime import REQUEST

    with engine.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar_one()
    with socket.socket() as socket_probe:
        socket_probe.bind(("127.0.0.1", 0))
        port = socket_probe.getsockname()[1]
    env = {
        **os.environ,
        "VIDEO_DATABASE_URL": engine.url.set(
            query={"options": "-csearch_path=" + schema}
        ).render_as_string(hide_password=False),
        "VIDEO_API_TOKEN": "test-process-token",
        "VIDEO_OWNER_ID": "alice",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "video_action_agent.api:create_env_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1) as client:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, "API process exited before ready"
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                assert time.monotonic() < deadline, "API startup timeout"
                time.sleep(0.05)
            headers = {"Authorization": "Bearer test-process-token", "Idempotency-Key": "http"}
            response = client.post("/tasks", json=REQUEST, headers=headers)
            assert response.status_code == 202
            task = client.get("/tasks/" + response.json()["task_id"], headers=headers).json()
            assert task["contract_state"] == "forming"
            assert task["completed"] is False
    finally:
        process.terminate()
        process.wait(timeout=15)
