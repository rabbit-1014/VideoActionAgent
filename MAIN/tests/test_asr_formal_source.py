"""Decisive formal ASR boundaries; all content here is an explicit test fixture."""

import json

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from video_action_agent.formation_handler import build_formation_command
from video_action_agent.local_transcriber import MODELS, PARAMETERS, SOFTWARE
from video_action_agent.models import TaskInput
from video_action_agent.runtime import Conflict

BVID = "BV1AJen6SEVQ"
PROOF = {
    "bvid": BVID, "page": 42, "cid": 41961066345,
    "part_title": "Part 42", "collection_title": "Course",
    "duration_seconds": 20.0, "stream_duration_seconds": 20.0,
    "observed_at": "2026-09-27T05:52:17+00:00",
    "access_mode": "anonymous_public_client",
    "identity_evidence": "view.pages + exact page_index/cid request + CDN path CID",
    "browser_session_ref": None,
}


def prepared_audio(runtime, engine, key="asr"):
    task = runtime.submit("alice", key, {
        "message": "整理视频中讲述的方法步骤和检查点", "source": f"https://www.bilibili.com/video/{BVID}/?p=42",
        "output_kind": "method_list", "source_mode": "audio_transcription",
        "local_source_actions": ["audio_transcription"],
    })
    claim = runtime.claim("forming", {"form_goal"}, allowed_owner="alice", task_id=task["task_id"])
    runtime.form_decision(claim, "alice", build_formation_command(claim))
    claim = runtime.claim("audio", {"read_source"}, allowed_owner="alice", task_id=task["task_id"])
    admission = runtime.prepare_source_read(claim, "alice", "audio_transcription")
    with engine.connect() as conn:
        attempt = conn.execute(text("SELECT attempt_id FROM source_operation_attempts WHERE operation_id=:op"),
                               {"op": admission.operation_id}).scalar_one()
    return task, admission, attempt


def envelope(proof=PROOF):
    return {"schema_version": 1, "source_evidence": proof, "transcription": {
        "kind": "audio_transcription", "audio_sha256": "a" * 64,
        "asr_input_sha256": "b" * 64, "asr_input_encoding": "pcm_f32le_16000_mono",
        "asr_input_hash_basis": "actual_worker_array", "duration": 20.0,
        "models": MODELS, "software": SOFTWARE, "timestamp_kind": "vad_segment",
        "parameters": PARAMETERS,
        "chunk_manifest": [{"index": 0, "start_sample": 0, "end_sample": 320000,
                            "start": 0.0, "end": 20.0, "asr_input_sha256": "b" * 64,
                            "status": "speech_recognized", "segment_count": 1,
                            "asr_seconds": 0.1, "diagnostic_file": "controlled-fixture"}],
        "chunk_coverage": {"kind": "all_input_samples_processed", "total_sample_count": 320000,
                           "chunk_count": 1, "empty_chunk_count": 0},
        "time_origin": "local_audio_first_decoded_sample", "automatically_verified_truth": False,
        "segments": [{"start": 1.0, "end": 3.0, "text": "创建实体和关系"}],
    }}


def settlement(admission, body=None):
    return {"operation_id": str(admission.operation_id), "action": "audio_transcription",
            "bvid": BVID, "page": 42, "adapter_version": "local-audio-asr-v1",
            "parse_version": "asr-envelope-v1", "raw_reply": json.dumps(body or envelope())}


def test_audio_requires_separate_explicit_scope(runtime, engine):
    with pytest.raises(ValidationError):
        TaskInput(message="methods", source=BVID, output_kind="method_list",
                  source_mode="audio_transcription", local_browser_actions=["subtitle"])
    from test_runtime import accepted_read_source_lease

    _, claim = accepted_read_source_lease(runtime, engine)
    with pytest.raises((Conflict, ValueError)):
        runtime.prepare_source_read(claim, "alice", "audio_transcription")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_reads")).scalar_one() == 0


def test_audio_consumed_dispatch_identity_and_provenance(runtime, engine):
    task, admission, attempt = prepared_audio(runtime, engine)
    ticket = runtime.admit_dispatch(admission.claim, "alice", admission.operation_id, attempt, PROOF)
    assert ticket.sendable
    assert not runtime.admit_dispatch(admission.claim, "alice", admission.operation_id,
                                      attempt, {**PROOF, "cid": 1}).sendable
    with pytest.raises(Conflict):
        runtime.settle_source_result(admission.claim, "alice", settlement(admission))
    assert runtime.consume_dispatch(admission.claim, "alice", ticket)
    changed = {**PROOF, "cid": 1}
    with pytest.raises(Conflict):
        runtime.settle_source_result(admission.claim, "alice", settlement(admission, envelope(changed)))
    result = runtime.settle_source_result(admission.claim, "alice", settlement(admission))
    materials = runtime.read_formal_materials("alice", task["task_id"])
    assert len(materials) == 1 and materials[0]["action"] == "audio_transcription"
    assert materials[0]["provenance"]["cid"] == PROOF["cid"]
    assert materials[0]["provenance"]["audio_sha256"] == "a" * 64
    assert materials[0]["provenance"]["models"] == MODELS
    assert materials[0]["provenance"]["automatically_verified_truth"] is False
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE source_formal_materials SET provenance=provenance-'kind'"))
    assert runtime.get("alice", task["task_id"])["completed"] is False
    assert runtime.settle_source_result(admission.claim, "alice", settlement(admission)).idempotent
    claim = runtime.claim("artifact", {"build_artifact"}, allowed_owner="alice", task_id=task["task_id"])
    assert claim.work_id == result.successor_work_id


def test_audio_recovery_preserves_scope_and_refuses_browser_proof(runtime, engine):
    from test_source_exit import PROOF as BROWSER_PROOF

    task, admission, _attempt = prepared_audio(runtime, engine, "asr-recovery")
    with engine.begin() as conn:
        conn.execute(text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second' WHERE work_id=:work"),
                     {"work": admission.claim.work_id})
    reconcile = runtime.claim("recovery", {"read_source"}, allowed_owner="alice",
                              task_id=task["task_id"], recover_expired=True)
    recovered = runtime.recover_source_read(reconcile, "alice")
    assert recovered.action == "attempt_recovered"
    assert recovered.operation_id == admission.operation_id
    assert not runtime.admit_dispatch(recovered.claim, "alice", recovered.operation_id,
                                      recovered.attempt_id, BROWSER_PROOF).sendable
    assert runtime.admit_dispatch(recovered.claim, "alice", recovered.operation_id,
                                  recovered.attempt_id, PROOF).sendable
    with engine.connect() as conn:
        decision = conn.execute(text("SELECT authorization_basis,entry_scope,policy_version FROM source_permission_decisions"))
        assert decision.one() == ("contract_local_source_actions", ["audio_transcription"],
                                  "accepted-local-bilibili-audio-asr-v1")
    from alembic.config import Config

    from alembic import command

    with engine.begin() as conn:
        config = Config("alembic.ini")
        config.attributes["connection"] = conn
        with pytest.raises(RuntimeError, match="audio transcription history"):
            command.downgrade(config, "0010")


def test_audio_canceled_long_read_cannot_record_late_material(runtime, engine):
    task, admission, attempt = prepared_audio(runtime, engine, "asr-cancel")
    ticket = runtime.admit_dispatch(admission.claim, "alice", admission.operation_id, attempt, PROOF)
    runtime.consume_dispatch(admission.claim, "alice", ticket)
    runtime.cancel("alice", task["task_id"], admission.claim.revision)
    with pytest.raises(Conflict):
        runtime.settle_source_result(admission.claim, "alice", settlement(admission))
    assert runtime.read_formal_materials("alice", task["task_id"]) == []
