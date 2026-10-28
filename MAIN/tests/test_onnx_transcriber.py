import copy
import json
import subprocess
import sys

import pytest

from video_action_agent.local_transcriber import TranscriptionError, validate_result


def result():
    return {
        "kind": "audio_transcription", "audio_sha256": "a" * 64,
        "asr_input_sha256": "b" * 64, "asr_input_encoding": "pcm_f32le_16000_mono",
        "asr_input_hash_basis": "actual_worker_array", "duration": 3.0,
        "models": {"asr": {
            "id": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
            "revision": "2365baeacb507f821a0c8120fcee3d484dba7a07",
            "weight_sha256": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
            "tokens_sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
        }, "vad": {"id": "k2-fsa/silero_vad.onnx",
                   "revision": "sha256:9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
                   "weight_sha256": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"}},
        "software": {"sherpa-onnx": "1.13.8", "sherpa-onnx-core": "1.13.8",
                     "numpy": "2.2.6", "soundfile": "0.13.1"},
        "parameters": {"language": "auto", "cpu_threads": 2, "use_itn": True,
                       "chunk_seconds": 20, "chunk_strategy": "fixed_nonoverlapping_windows",
                       "decoding_method": "greedy_search", "quantization": "int8",
                       "vad": {"threshold": 0.5, "min_silence_duration": 0.5,
                               "min_speech_duration": 0.25, "max_speech_duration": 20,
                               "window_size": 512, "num_threads": 1, "mode": "window_presence_gate"}},
        "timestamp_kind": "fixed_audio_window", "time_origin": "local_audio_first_decoded_sample",
        "automatically_verified_truth": False,
        "segments": [{"start": 0.0, "end": 3.0, "text": "完整保留这段语音"}],
    }


def test_pinned_onnx_result_is_accepted_without_claiming_vad_precision():
    data = result()
    before = copy.deepcopy(data)
    assert validate_result(data, "a" * 64, 4) == data
    assert data == before


def test_changed_onnx_model_and_false_precision_are_rejected():
    for field, value in [("timestamp_kind", "vad_segment"), ("software", {"sherpa-onnx": "latest"})]:
        data = result()
        data[field] = value
        with pytest.raises(TranscriptionError):
            validate_result(data, "a" * 64, 4)
    data = result()
    data["models"]["asr"]["weight_sha256"] = "c" * 64
    with pytest.raises(TranscriptionError):
        validate_result(data, "a" * 64, 4)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows worker memory accounting")
def test_resource_guard_stops_owned_worker_without_publishing_transcript(tmp_path):
    output, diagnostic = tmp_path / "result.json", tmp_path / "resources.json"
    code = (
        "import sys,time;from pathlib import Path;"
        "from video_action_agent.asr_worker_resources import start_guard;"
        "start_guard(Path(sys.argv[1]),Path(sys.argv[2]),private_budget=1);time.sleep(3)"
    )
    child = subprocess.run([sys.executable, "-c", code, str(output), str(diagnostic)],
                           capture_output=True, timeout=10, check=False)
    assert child.returncode == 2
    failure = json.loads(output.read_text(encoding="utf-8"))
    assert failure == {"error": "transcription_failed", "rejected_field": "worker_resource_budget"}
    assert "segments" not in failure
    assert json.loads(diagnostic.read_text(encoding="utf-8"))["private_bytes"] > 1


def test_speech_gate_stops_when_positive_and_still_checks_a_silent_tail():
    from video_action_agent.onnx_transcriber import speech_present

    class Detector:
        def __init__(self, positive_at=None, tail=False):
            self.positive_at, self.tail, self.calls, self.flushed = positive_at, tail, 0, False

        def reset(self):
            self.calls, self.flushed = 0, False

        def accept_waveform(self, samples):
            self.calls += 1

        def is_speech_detected(self):
            return self.calls == self.positive_at

        def empty(self):
            return not (self.flushed and self.tail)

        def flush(self):
            self.flushed = True

    positive = Detector(positive_at=2)
    assert speech_present(positive, list(range(20)), frame_size=4)
    assert positive.calls == 2 and not positive.flushed
    for tail in (False, True):
        detector = Detector(tail=tail)
        assert speech_present(detector, list(range(20)), frame_size=4) is tail
        assert detector.calls == 5 and detector.flushed
