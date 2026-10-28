"""Decisive counterexamples for the controlled formal source exit (GOAL-015).

Only the counterexamples that prove it is safe to continue are kept: no send
right without observed proof or a live lease; at most one dispatch under
concurrency and resend; valid subtitles form material and exactly one successor
atomically; a possibly-dispatched timeout/crash never retries and never strands a
Goal; an explicit failure is never written up as "no subtitles".
"""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from test_runtime import accepted_read_source_lease

from video_action_agent.runtime import Conflict
from video_action_agent.source_access import ADAPTER_VERSION

BVID = "BV1gD421u75J"
RAW_SUBTITLES = json.dumps(
    [
        {"index": 1, "from": "2.06s", "to": "4.54s", "content": "剪映给视频加字"},
        {"index": 2, "from": "5.82s", "to": "11.64s", "content": "这期教大家用剪映给视频加字幕方法有三种"},
    ]
)

PROOF = {
    "schema_version": 1,
    "observed_at": "2026-09-23T12:00:00+08:00",
    "adapter_version": ADAPTER_VERSION,
    "browser_session_ref": "chrome-eca7gafg-session-01",
    "observed_connection_state": "verified",
    "observation_command": "opencli doctor --json",
    "observation_return": "Extension connected v1.0.24",
}


def prepared(runtime, engine, actions=("subtitle",), key="scoped"):
    """Controlled setup only: accepted task plus one prepared formal operation."""
    task, lease = accepted_read_source_lease(runtime, engine, actions=actions, key=key)
    admission = runtime.prepare_source_read(lease, "alice", actions[0])
    with engine.connect() as conn:
        attempt = conn.execute(
            text("SELECT * FROM source_operation_attempts WHERE operation_id=:op"),
            {"op": admission.operation_id},
        ).mappings().one()
    return task, admission, attempt


def sendable(runtime, claim, operation_id, attempt_id, proof=PROOF):
    return runtime.admit_dispatch(claim, "alice", operation_id, attempt_id, proof)


def settlement(operation_id, action="subtitle", raw=RAW_SUBTITLES, parse="parse-v0"):
    return {
        "schema_version": 1,
        "operation_id": str(operation_id),
        "action": action,
        "bvid": BVID,
        "page": 1,
        "raw_reply": raw,
        "adapter_version": ADAPTER_VERSION,
        "parse_version": parse,
    }


def unknown(operation_id, code="adapter_timeout", paths=(), waiting=None):
    return {
        "schema_version": 1,
        "operation_id": str(operation_id),
        "code": code,
        "evidence": "LocalOpenCli.fetch raised a timeout after the dispatch call was issued",
        "remaining_legal_paths": list(paths),
        "waiting_condition": waiting,
    }


# 1. No send right without an observed session proof or a live lease.
def test_absent_or_unverifiable_session_proof_never_grants_a_send_right(runtime, engine):
    _task, admission, attempt = prepared(runtime, engine)
    claim = admission.claim

    blocked = runtime.admit_dispatch(claim, "alice", admission.operation_id, attempt["attempt_id"], None)
    assert blocked.sendable is False
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM source_dispatch_admissions")).mappings().all()
        phase = conn.execute(
            text("SELECT phase FROM source_reads WHERE operation_id=:op"),
            {"op": admission.operation_id},
        ).scalar_one()
    assert [(r["state"], r["checked_connection_state"]) for r in rows] == [("refused", "unverified")]
    assert rows[0]["refusal_code"] == "session_proof_absent"
    assert phase == "prepared", "a refusal must not mark the operation dispatched"

    # A permission decision recorded as unverified / auto_allow is not proof: the
    # proof is rejected and the ticket is not sendable.
    forged = {**PROOF, "observation_return": "connection_state=unverified"}
    rejected = runtime.admit_dispatch(
        claim, "alice", admission.operation_id, attempt["attempt_id"], forged
    )
    assert rejected.sendable is False


def test_forged_lease_cannot_obtain_dispatch_admission(runtime, engine):
    _, admission, attempt = prepared(runtime, engine)
    forged = type(admission.claim)(**{**admission.claim.__dict__, "token": attempt["lease_token"]})
    impostor = type(admission.claim)(
        **{**admission.claim.__dict__, "token": __import__("uuid").uuid4()}
    )
    with pytest.raises(Conflict):
        runtime.admit_dispatch(impostor, "alice", admission.operation_id, attempt["attempt_id"], PROOF)
    with pytest.raises(Conflict):
        runtime.admit_dispatch(admission.claim, "bob", admission.operation_id, attempt["attempt_id"], PROOF)
    assert forged is not None


# 2. Concurrency and resend produce at most one dispatch.
def test_dispatch_admission_is_single_use_under_concurrency_and_resend(runtime, engine):
    _task, admission, attempt = prepared(runtime, engine)
    claim = admission.claim

    def once(_):
        return runtime.admit_dispatch(claim, "alice", admission.operation_id, attempt["attempt_id"], PROOF)

    with ThreadPoolExecutor(max_workers=4) as pool:
        tickets = list(pool.map(once, range(4)))
    sendable_ids = {t.admission_id for t in tickets if t.sendable}
    assert len(sendable_ids) == 1, "concurrent callers must share one send right"
    assert {t.dispatch_generation for t in tickets} == {1}

    again = once(None)
    assert again.admission_id == tickets[0].admission_id
    with engine.connect() as conn:
        granted = conn.execute(
            text("SELECT count(*) FROM source_dispatch_admissions WHERE state='granted'")
        ).scalar_one()
        dispatching = conn.execute(
            text("SELECT phase, dispatch_generation FROM source_reads WHERE operation_id=:op"),
            {"op": admission.operation_id},
        ).one()
    assert granted == 1
    assert (dispatching[0], dispatching[1]) == ("dispatching", 1)


# 3. Valid subtitles form formal material and exactly one successor, atomically.
def test_valid_subtitles_form_material_and_single_successor_atomically(runtime, engine):
    _task, admission, attempt = prepared(runtime, engine)
    claim = admission.claim
    ticket = sendable(runtime, claim, admission.operation_id, attempt["attempt_id"])
    assert ticket.sendable is True

    result = runtime.settle_source_result(claim, "alice", settlement(admission.operation_id))
    assert result.disposition == "recorded"
    assert result.expected_revision == claim.revision + 1
    assert result.segments == 2
    assert result.successor_work_id is not None

    with engine.connect() as conn:
        material = conn.execute(text("SELECT * FROM source_formal_materials")).mappings().one()
        work = conn.execute(
            text("SELECT kind,state,expected_revision FROM work_items ORDER BY created_at")
        ).mappings().all()
        transition = conn.execute(
            text("SELECT revision,dedup_key FROM transitions WHERE dedup_key LIKE 'source-exit:%'")
        ).mappings().all()
    assert material["material_state"] == "recorded"
    assert material["successor_work_id"] == result.successor_work_id
    assert [w["kind"] for w in work] == ["form_goal", "read_source", "build_artifact"]
    assert work[1]["state"] == "closed" and work[2]["state"] == "ready"
    assert work[2]["expected_revision"] == result.expected_revision
    keys = [t["dedup_key"] for t in transition]
    assert keys[-1] == f"source-exit:{admission.operation_id}"
    assert transition[-1]["revision"] == result.expected_revision

    # Resend of the identical body is idempotent; a different body conflicts.
    assert runtime.settle_source_result(claim, "alice", settlement(admission.operation_id)).idempotent
    with pytest.raises(Conflict):
        runtime.settle_source_result(
            claim, "alice", settlement(admission.operation_id, raw=RAW_SUBTITLES + "X")
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_formal_materials")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM work_items WHERE kind='build_artifact'")
                            ).scalar_one() == 1


def test_same_video_reply_can_supply_two_independent_authorized_tasks(runtime, engine):
    first_task, first, first_attempt = prepared(runtime, engine, key="same-video-first")
    sendable(runtime, first.claim, first.operation_id, first_attempt["attempt_id"])
    first_result = runtime.settle_source_result(first.claim, "alice", settlement(first.operation_id))

    second_task, second, second_attempt = prepared(runtime, engine, key="same-video-second")
    sendable(runtime, second.claim, second.operation_id, second_attempt["attempt_id"])
    second_result = runtime.settle_source_result(second.claim, "alice", settlement(second.operation_id))

    assert first_task["task_id"] != second_task["task_id"]
    assert first_result.material_id != second_result.material_id
    assert first_result.payload_hash == second_result.payload_hash
    with engine.connect() as conn:
        materials = conn.execute(
            text("SELECT task_id, operation_id FROM source_formal_materials ORDER BY recorded_at")
        ).all()
    assert {row[0] for row in materials} == {first_task["task_id"], second_task["task_id"]}
    assert len({row[1] for row in materials}) == 2


def test_successor_write_failure_rolls_back_the_whole_exit(runtime, engine):
    from sqlalchemy import event

    _task, admission, attempt = prepared(runtime, engine)
    claim = admission.claim
    sendable(runtime, claim, admission.operation_id, attempt["attempt_id"])
    before = runtime.get("alice", admission.claim.task_id)["revision"]

    def break_successor(conn, cursor, statement, params, context, many):
        if statement.lstrip().startswith("INSERT INTO work_items"):
            raise RuntimeError("injected successor write failure")

    event.listen(engine, "before_cursor_execute", break_successor)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            runtime.settle_source_result(claim, "alice", settlement(admission.operation_id))
    finally:
        event.remove(engine, "before_cursor_execute", break_successor)

    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_formal_materials")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM work_items WHERE kind='build_artifact'")
                            ).scalar_one() == 0
        phase = conn.execute(text("SELECT phase FROM source_reads")).scalar_one()
    assert phase == "dispatching", "the exit must not be half-applied"
    assert runtime.get("alice", admission.claim.task_id)["revision"] == before


# 4. A possibly-dispatched unknown never retries and never strands a Goal.
def test_unknown_after_possible_dispatch_closes_without_retry_or_hanging_goal(runtime, engine):
    _task, admission, attempt = prepared(runtime, engine)
    claim = admission.claim
    sendable(runtime, claim, admission.operation_id, attempt["attempt_id"])

    closed = runtime.settle_source_unknown(claim, "alice", unknown(admission.operation_id))
    assert closed.outcome == "outcome_unknown"
    assert closed.goals_indeterminate == 1
    assert closed.closed_work is True

    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM source_reads")).mappings().one()
        goals = conn.execute(text("SELECT state FROM goals WHERE required")).scalars().all()
        work = conn.execute(text("SELECT state FROM work_items")).scalars().all()
    assert row["outcome"] == "outcome_unknown", "a timeout is never 'no subtitles'"
    assert row["material"] is None and row["payload_hash"] is None
    assert row["outcome_basis"] == "adapter_timeout"
    assert set(goals) == {"indeterminate"}, "no active Goal may be left hanging"
    assert set(work) == {"closed"}, "no executable work may be left hanging"

    # Unknown is terminal and never auto-retried.
    with pytest.raises(Conflict):
        runtime.settle_source_result(claim, "alice", settlement(admission.operation_id))


def test_unknown_with_a_real_remaining_path_keeps_the_goal_active(runtime, engine):
    _task, admission, attempt = prepared(runtime, engine)
    claim = admission.claim
    sendable(runtime, claim, admission.operation_id, attempt["attempt_id"])
    closed = runtime.settle_source_unknown(
        claim,
        "alice",
        unknown(
            admission.operation_id,
            paths=["authoritative per-operation query, once available"],
            waiting="Awaiting the authoritative per-operation query interface",
        ),
    )
    assert closed.goals_indeterminate == 0
    with engine.connect() as conn:
        goals = conn.execute(text("SELECT state FROM goals WHERE required")).scalars().all()
        blockers = conn.execute(
            text("SELECT state,waiting_condition FROM goal_blockers")
        ).mappings().all()
        work = conn.execute(
            text("SELECT count(*) FROM work_items WHERE state IN ('ready','leased')")
        ).scalar_one()
    assert set(goals) == {"active"}
    assert not work
    assert [b["state"] for b in blockers] == ["open"]
    assert "authoritative" in blockers[0]["waiting_condition"]


# 5. An explicit failure is real evidence, never written up as "no subtitles".
def test_explicit_failure_keeps_real_evidence_and_is_not_missing_subtitles(runtime, engine):
    _task, admission, attempt = prepared(runtime, engine)
    claim = admission.claim
    sendable(runtime, claim, admission.operation_id, attempt["attempt_id"])
    with pytest.raises(ValueError):
        runtime.settle_source_failure(claim, "alice", admission.operation_id, "adapter_failed", "")

    closed = runtime.settle_source_failure(
        claim,
        "alice",
        admission.operation_id,
        "adapter_failed",
        "OpenCLI exited 1 with navigation rejected for this BVID",
    )
    assert closed.outcome == "failed"
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM source_reads")).mappings().one()
        materials = conn.execute(text("SELECT count(*) FROM source_formal_materials")).scalar_one()
        goals = conn.execute(text("SELECT state FROM goals WHERE required")).scalars().all()
    assert row["outcome"] == "failed"
    assert "no subtitle" not in (row["outcome_basis"] or "").lower()
    assert "navigation rejected" in row["outcome_evidence"]
    assert materials == 0, "a failed read forms no formal material"
    assert set(goals) == {"indeterminate"}, "the last safe path closed honestly"


# Formal material reading stays separate from the diagnostic forming query.
def test_formal_material_read_is_versioned_and_separated_from_diagnostics(runtime, engine):
    from video_action_agent.source_runtime import SourceRuntime

    _task, admission, attempt = prepared(runtime, engine)
    claim = admission.claim
    sendable(runtime, claim, admission.operation_id, attempt["attempt_id"])
    runtime.settle_source_result(claim, "alice", settlement(admission.operation_id))

    materials = runtime.read_formal_materials("alice", admission.claim.task_id)
    assert len(materials) == 1
    assert materials[0]["material_state"] == "recorded"
    assert [s["text"] for s in materials[0]["segments"]] == [
        "剪映给视频加字",
        "这期教大家用剪映给视频加字幕方法有三种",
    ]
    assert materials[0]["segments"][0]["start"] == 2.06
    assert materials[0]["payload_hash"]
    assert runtime.read_formal_materials("bob", admission.claim.task_id) == []
    # The diagnostic forming query is not widened by the formal interface.
    assert SourceRuntime(runtime, "alice", True).current("alice", admission.claim.task_id) == []


def test_build_artifact_successor_is_visible_and_claimable_for_its_handler(runtime, engine):
    _task, admission, attempt = prepared(runtime, engine)
    claim = admission.claim
    sendable(runtime, claim, admission.operation_id, attempt["attempt_id"])
    result = runtime.settle_source_result(claim, "alice", settlement(admission.operation_id))

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT kind,state FROM work_items WHERE work_id=:work"),
            {"work": result.successor_work_id},
        ).one()
    assert (row[0], row[1]) == ("build_artifact", "ready")

    # _view shows the current work instead of the shadowing closed row.
    view = runtime.get("alice", admission.claim.task_id)
    assert view["work_kind"] == "build_artifact"
    assert view["work_state"] == "ready"
    assert runtime.claim("w3", {"build_artifact"}, task_id=admission.claim.task_id) is not None


def test_dispatch_ticket_is_consumed_once_before_external_send(runtime, engine):
    _task, admission, attempt = prepared(runtime, engine)
    ticket = sendable(runtime, admission.claim, admission.operation_id, attempt["attempt_id"])
    assert runtime.consume_dispatch(admission.claim, "alice", ticket) is True
    assert runtime.consume_dispatch(admission.claim, "alice", ticket) is False
    resent = sendable(runtime, admission.claim, admission.operation_id, attempt["attempt_id"])
    assert resent.sendable is False
