"""Controlled full ASR wiring without real audio, model, or development DB."""

import hashlib
from pathlib import Path

import pytest
from test_asr_artifact_context import Generator as SmallDraftProvider
from test_asr_artifact_context import Reviewer
from test_asr_formal_source import BVID, PROOF, envelope

from video_action_agent.artifact_quality import review_method_list
from video_action_agent.local_transcriber import TranscriptionError
from video_action_agent.product_pipeline import ProductPipeline
from video_action_agent.runtime import Conflict


class Session:
    calls = 0

    def __init__(self, ref, on_poll):
        self.ref, self.on_poll = ref, on_poll

    def resolve(self):
        self.on_poll()
        return PROOF

    def download(self, destination):
        self.on_poll()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"explicit-test-audio")
        type(self).calls += 1
        return {**PROOF, "local_path": str(destination),
                "audio_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}

    def close(self):
        pass


def transcriber(path, *, on_poll, **_):
    on_poll()
    result = envelope()["transcription"]
    result["audio_sha256"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    return result


def task(runtime, key):
    return runtime.submit("alice", key, {
        "message": "整理这个视频的方法步骤和检查点",
        "source": f"https://www.bilibili.com/video/{BVID}/?p=42",
        "source_mode": "audio_transcription", "local_source_actions": ["audio_transcription"],
        "output_kind": "method_list",
    })


def test_explicit_audio_path_reaches_saved_candidate_with_source_identity(runtime, tmp_path):
    item = task(runtime, "audio-pipeline")
    pipeline = ProductPipeline(runtime, "alice", None, None, SmallDraftProvider(),
                               audio_session_factory=Session, transcriber=transcriber,
                               audio_output_root=tmp_path)
    assert pipeline.run_once(item["task_id"])["decision"] == "accept"
    assert pipeline.run_once(item["task_id"])["state"] == "recorded"
    assert pipeline.run_once(item["task_id"])["state"] == "unreviewed_candidate"
    artifact = runtime.get_artifact("alice", item["task_id"])
    assert artifact["draft"]["source_ref"]["kind"] == "audio_transcription"
    assert artifact["draft"]["source_ref"]["cid"] == PROOF["cid"]
    assert "ASR" in artifact["markdown"]
    material = runtime.read_formal_materials("alice", item["task_id"])[0]
    review = review_method_list(artifact["draft"], material["segments"],
                               artifact["draft"]["user_request"], artifact["draft"]["source_ref"],
                               material["payload_hash"], Reviewer())
    runtime.save_artifact_review("alice", item["task_id"], artifact["artifact_id"],
                                 artifact["revision"], review)
    assert runtime.get_artifact("alice", item["task_id"])["reviews"][0]["review"]["source_ref"]["cid"] == PROOF["cid"]
    assert not runtime.get("alice", item["task_id"])["completed"]


def test_long_audio_stage_cancellation_stops_next_stage(runtime, tmp_path):
    item = task(runtime, "audio-cancel-pipeline")
    called = False

    class CanceledSession(Session):
        def download(self, destination):
            result = super().download(destination)
            runtime.cancel("alice", item["task_id"], runtime.get("alice", item["task_id"])["revision"])
            return result

    def forbidden_transcriber(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Canceled task reached ASR")

    pipeline = ProductPipeline(runtime, "alice", None, None, SmallDraftProvider(),
                               audio_session_factory=CanceledSession, transcriber=forbidden_transcriber,
                               audio_output_root=tmp_path)
    pipeline.run_once(item["task_id"])
    with pytest.raises(Conflict):
        pipeline.run_once(item["task_id"])
    assert not called and runtime.read_formal_materials("alice", item["task_id"]) == []


def test_asr_no_recognized_speech_closes_honestly_without_successor(runtime, tmp_path):
    item = task(runtime, "audio-no-speech")

    def silence(*args, **kwargs):
        raise TranscriptionError("no_speech_recognized")

    pipeline = ProductPipeline(runtime, "alice", None, None, SmallDraftProvider(),
                               audio_session_factory=Session, transcriber=silence,
                               audio_output_root=tmp_path)
    pipeline.run_once(item["task_id"])
    assert pipeline.run_once(item["task_id"]) == {
        "stage": "source", "state": "failed", "code": "no_speech_recognized",
    }
    assert pipeline.run_once(item["task_id"])["stage"] == "idle"
    assert runtime.read_formal_materials("alice", item["task_id"]) == []
    view = runtime.get("alice", item["task_id"])
    assert not view["completed"] and view["work_kind"] != "build_artifact"
