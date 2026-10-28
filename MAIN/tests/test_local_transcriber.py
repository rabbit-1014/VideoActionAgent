import importlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


def api():
    assert importlib.util.find_spec("video_action_agent.local_transcriber"), (
        "local transcriber missing"
    )
    return importlib.import_module("video_action_agent.local_transcriber")


def reply(module, sha):
    return {
        "kind": "audio_transcription",
        "audio_sha256": sha,
        "asr_input_sha256": "c" * 64,
        "asr_input_encoding": "pcm_f32le_16000_mono",
        "asr_input_hash_basis": "actual_worker_array",
        "duration": 3.0,
        "models": module.MODELS,
        "software": module.SOFTWARE,
        "parameters": module.PARAMETERS,
        "timestamp_kind": "vad_segment",
        "time_origin": "local_audio_first_decoded_sample",
        "automatically_verified_truth": False,
        "segments": [{"start": 0.1, "end": 2.0, "text": "中文方法"}],
    }


@pytest.mark.parametrize("corruption", ["identity", "time", "hash", "truth", "pcm"])
def test_untrusted_worker_result_rejected(corruption):
    module = api()
    data = reply(module, "a" * 64)
    if corruption == "identity":
        data["models"] = {"untrusted": "model"}
    elif corruption == "time":
        data["segments"][0]["end"] = 3.01
    elif corruption == "hash":
        data["audio_sha256"] = "b" * 64
    elif corruption == "truth":
        data["automatically_verified_truth"] = True
    else:
        data["asr_input_sha256"] = "invalid"
    with pytest.raises(module.TranscriptionError) as error:
        module.validate_result(data, "a" * 64, 60)
    assert error.value.code == "invalid_worker_result"


def test_decode_failure_is_stable_and_not_no_content():
    module = api()
    with pytest.raises(module.TranscriptionError) as error:
        module.validate_result({"error": "decode_failed"}, "a" * 64, 60)
    assert error.value.code == "decode_failed"


def test_timeout_once_no_retry(tmp_path, monkeypatch):
    module = api()
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"local bytes")
    calls = []

    class Expired:
        pid = 12345

        def __init__(self, *args, **kwargs):
            calls.append(args)
            self.waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("worker", timeout)
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", Expired)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(module.os, "killpg", lambda *args: None, raising=False)
    with pytest.raises(module.TranscriptionError) as error:
        module.transcribe_local(audio, python_path=audio, worker_script=audio, timeout_seconds=1)
    assert error.value.code == "transcription_timeout"
    assert len(calls) == 1


def test_valid_candidate_preserves_evidence():
    module = api()
    data = reply(module, "a" * 64)
    assert module.validate_result(data, "a" * 64, 60) == data


def test_urls_rejected_before_process(monkeypatch):
    module = api()

    def forbidden(*args, **kwargs):
        pytest.fail("URL reached subprocess")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    with pytest.raises(module.TranscriptionError) as error:
        module.transcribe_local("https://example.com/audio.m4a")
    assert error.value.code == "invalid_local_input"


def test_cancelled_before_launch_never_starts_worker(tmp_path, monkeypatch):
    module = api()
    audio = tmp_path / "local.wav"
    audio.write_bytes(b"local audio")

    def forbidden(*args, **kwargs):
        pytest.fail("cancelled work started")

    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    with pytest.raises(module.TranscriptionError) as error:
        module.transcribe_local(audio, cancel_check=lambda: True)
    assert error.value.code == "transcription_cancelled"


def test_lease_callback_failure_stops_worker_and_propagates(tmp_path, monkeypatch):
    module = api()
    audio = tmp_path / "local.wav"
    audio.write_bytes(b"local audio")
    stopped = []

    class Running:
        pid = 12345

        def __init__(self, *args, **kwargs):
            pass

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(module.subprocess, "Popen", Running)
    monkeypatch.setattr(module, "_stop_worker", lambda process: stopped.append(process.pid))

    def lease_lost():
        raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        module.transcribe_local(audio, python_path=audio, worker_script=audio, on_poll=lease_lost)
    assert stopped == [12345]


def test_rejected_asr_candidate_kept_only_as_local_diagnostic(tmp_path):
    module = api()
    spec = importlib.util.spec_from_file_location(
        "asr_cli_test", Path(__file__).resolve().parents[1] / "scripts/asr_transcribe.py"
    )
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    candidate = reply(module, "a" * 64)
    candidate["segments"][0]["end"] = 4.0
    with pytest.raises(module.TranscriptionError) as error:
        cli.checked_candidate(candidate, [{"text": "raw candidate"}], "a" * 64, 60, tmp_path)
    assert error.value.code == "invalid_worker_result"
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    diagnostic = json.loads(files[0].read_text(encoding="utf-8"))
    assert diagnostic["candidate"]["automatically_verified_truth"] is False
    assert diagnostic["validation_error"] == "invalid_worker_result"
    assert diagnostic["rejected_field"] == "segments[0].bounds"


def test_window_offset_uses_actual_samples_and_empty_window_adds_no_words():
    module = api()
    raw = [{"sentence_info": [{"start": 100, "end": 19800, "text": "<|en|>Words"}]}]
    assert module.normalize_window(raw, 320000, 640000) == [
        {"start": 20.1, "end": 39.8, "text": "Words"}
    ]
    assert module.normalize_window([{"text": "", "timestamp": []}], 640000, 960000) == []


def test_empty_window_is_recorded_and_missing_window_rejected():
    module = api()
    data = reply(module, "a" * 64)
    data["duration"] = 40.0
    data["chunk_manifest"] = [
        {
            "index": 0,
            "start_sample": 0,
            "end_sample": 320000,
            "start": 0.0,
            "end": 20.0,
            "asr_input_sha256": "b" * 64,
            "status": "speech_recognized",
            "segment_count": 1,
            "asr_seconds": 1.0,
            "diagnostic_file": "local-chunk-0.json",
        },
        {
            "index": 1,
            "start_sample": 320000,
            "end_sample": 640000,
            "start": 20.0,
            "end": 40.0,
            "asr_input_sha256": "c" * 64,
            "status": "empty",
            "segment_count": 0,
            "asr_seconds": 1.0,
            "diagnostic_file": "local-chunk-1.json",
        },
    ]
    data["chunk_coverage"] = {
        "kind": "all_input_samples_processed",
        "total_sample_count": 640000,
        "chunk_count": 2,
        "empty_chunk_count": 1,
    }
    assert module.validate_result(data, "a" * 64, 60)["chunk_manifest"][1]["status"] == "empty"
    data["chunk_manifest"].pop()
    with pytest.raises(module.TranscriptionError) as error:
        module.validate_result(data, "a" * 64, 60)
    assert error.value.rejected_field == "chunk_manifest.coverage"
