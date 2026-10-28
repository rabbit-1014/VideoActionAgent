import hashlib
import json

import pytest

from video_action_agent.content_intake import IntakeError, capture, reuse
from video_action_agent.local_transcriber import TranscriptionError

SOURCE = "https://www.bilibili.com/video/BV1AJen6SEVQ/?p=42"
PROOF = {"bvid": "BV1AJen6SEVQ", "page": 42, "cid": 41961066345,
         "part_title": "part42", "duration_seconds": 20.0}


def fixtures(monkeypatch, tmp_path, fail_asr=False):
    calls = []
    result = {"audio_sha256": hashlib.sha256(b"test audio").hexdigest(), "segments": []}

    class Session:
        def __init__(self, ref, **kwargs):
            calls.append("session")

        def resolve(self):
            calls.append("resolve")
            return PROOF

        def download(self, path):
            calls.append("download")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test audio")
            return {**PROOF, "audio_sha256": result["audio_sha256"]}

        def close(self):
            calls.append("close")

    def transcribe(path, **kwargs):
        calls.append("asr")
        assert kwargs["timeout_seconds"] == 900
        assert kwargs["max_duration"] == 7200
        if fail_asr:
            raise TranscriptionError("transcription_failed")
        return result

    def build(envelope, ref, acquisition, audio_path):
        calls.append("build")
        return {"source_envelope": envelope, "acquisition": acquisition}

    def save(bundle, output_dir):
        calls.append("save")
        return {"state": "unverified_source_candidate", "acquisition": bundle["acquisition"],
                "segments": 0, "paths": {"candidate": str(output_dir / "candidate.json")}}

    monkeypatch.setattr("video_action_agent.content_intake.AudioSession", Session)
    monkeypatch.setattr("video_action_agent.content_intake.transcribe_local", transcribe)
    monkeypatch.setattr("video_action_agent.content_intake._build_bundle", build)
    monkeypatch.setattr("video_action_agent.content_intake._save_bundle", save)
    monkeypatch.setattr("video_action_agent.content_intake.INTAKE_ROOT", tmp_path / "intake")
    return calls, result


def test_missing_flag_and_wrong_ref_do_not_start_external(monkeypatch, tmp_path):
    calls, _ = fixtures(monkeypatch, tmp_path)
    with pytest.raises(IntakeError, match="audio_permission_required"):
        capture(SOURCE, tmp_path / "out", allow_local_audio_read=False)
    with pytest.raises(IntakeError, match="invalid_source"):
        capture("https://example.com/video/no", tmp_path / "out", allow_local_audio_read=True)
    assert calls == []


def test_capture_stages_save_candidate_and_close(monkeypatch, tmp_path):
    calls, _ = fixtures(monkeypatch, tmp_path)
    summary = capture(SOURCE, tmp_path / "out", allow_local_audio_read=True)
    assert calls == ["session", "resolve", "download", "asr", "build", "save", "close"]
    assert summary["completed"] is False
    assert summary["formal_task"] is False
    assert summary["acquisition"] == "live_anonymous_audio"


def test_reuse_has_no_external_and_checks_audio(monkeypatch, tmp_path):
    calls, result = fixtures(monkeypatch, tmp_path)
    audio = tmp_path / "source.m4a"
    audio.write_bytes(b"test audio")
    envelope = tmp_path / "source.json"
    envelope.write_text(json.dumps({"source_evidence": PROOF, "transcription": result}))
    assert reuse(SOURCE, envelope, audio, tmp_path / "out")["acquisition"] == "recorded_audio"
    assert calls == ["build", "save"]
    audio.write_bytes(b"modified")
    with pytest.raises(IntakeError, match="audio_integrity_mismatch"):
        reuse(SOURCE, envelope, audio, tmp_path / "out2")
    assert calls == ["build", "save"]


def test_asr_failure_no_save_no_retry_and_close(monkeypatch, tmp_path):
    calls, _ = fixtures(monkeypatch, tmp_path, fail_asr=True)
    with pytest.raises(IntakeError, match="transcription_failed"):
        capture(SOURCE, tmp_path / "out", allow_local_audio_read=True)
    assert calls == ["session", "resolve", "download", "asr", "close"]
    assert not (tmp_path / "out").exists()


def test_existing_output_refused_before_external(monkeypatch, tmp_path):
    calls, _ = fixtures(monkeypatch, tmp_path)
    output = tmp_path / "out"
    output.mkdir()
    with pytest.raises(IntakeError, match="output_exists"):
        capture(SOURCE, output, allow_local_audio_read=True)
    assert calls == []


