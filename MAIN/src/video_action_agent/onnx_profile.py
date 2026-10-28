"""Pinned alternative local recognizer; never relabel its times as VAD."""
from pathlib import Path

ROOT = Path("D:/VideoActionAgent-asr-pilot/onnx-20261023")
MODELS = {"asr": {
    "id": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
    "revision": "2365baeacb507f821a0c8120fcee3d484dba7a07",
    "weight_sha256": "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51",
    "tokens_sha256": "f449eb28dc567533d7fa59be34e2abca8784f771850c78a47fb731a31429a1dc",
}, "vad": {"id": "k2-fsa/silero_vad.onnx",
           "revision": "sha256:9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6",
           "weight_sha256": "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"}}
SOFTWARE = {"sherpa-onnx": "1.13.8", "sherpa-onnx-core": "1.13.8",
            "numpy": "2.2.6", "soundfile": "0.13.1"}
PARAMETERS = {"language": "auto", "cpu_threads": 2, "use_itn": True,
              "chunk_seconds": 20, "chunk_strategy": "fixed_nonoverlapping_windows",
              "decoding_method": "greedy_search", "quantization": "int8",
              "vad": {"threshold": 0.5, "min_silence_duration": 0.5,
                      "min_speech_duration": 0.25, "max_speech_duration": 20,
                      "window_size": 512, "num_threads": 1, "mode": "window_presence_gate"}}
TIMESTAMP_KIND = "fixed_audio_window"
