"""Local audio transcription candidate boundary; never platform subtitles."""

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

PILOT_ROOT = Path("D:/VideoActionAgent-asr-pilot")
MODELS = {
    "asr": {
        "id": "FunAudioLLM/SenseVoiceSmall",
        "revision": "3847d57b6bdf2dd8875cb1508d2af43d80a16bf7",
        "weight_sha256": "833ca2dcfdf8ec91bd4f31cfac36d6124e0c459074d5e909aec9cabe6204a3ea",
    },
    "vad": {
        "id": "funasr/fsmn-vad",
        "revision": "df20e6b30c653645fa4ff125cacfcabd1020a669",
        "weight_sha256": "b3be75be477f0780277f3bae0fe489f48718f585f3a6e45d7dd1fbb1a4255fc5",
    },
}
SOFTWARE = {
    "funasr": "1.4.16",
    "torch": "2.8.0+cpu",
    "torchaudio": "2.8.0+cpu",
    "imageio-ffmpeg": "0.6.0",
}
PARAMETERS = {
    "language": "auto",
    "cpu_threads": 4,
    "use_itn": True,
    "batch_size_s": 60,
    "merge_vad": False,
    "sentence_timestamp": True,
    "chunk_seconds": 20,
    "chunk_strategy": "fixed_nonoverlapping_windows",
}
ERROR_CODES = {
    "decode_failed",
    "empty_audio",
    "no_speech_recognized",
    "duration_limit",
    "model_identity_mismatch",
    "transcription_failed",
    "input_changed",
    "transcription_timeout",
    "invalid_worker_result",
}


class TranscriptionError(Exception):
    def __init__(self, code: str, rejected_field: str | None = None):
        self.code = code
        self.rejected_field = rejected_field
        super().__init__(code)


def audio_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def local_audio_path(value: str | Path, max_bytes: int) -> Path:
    raw = str(value)
    if "://" in raw or raw.startswith(("\\\\", "//")):
        raise TranscriptionError("invalid_local_input")
    path = Path(raw).resolve()
    if str(path).startswith(("\\\\", "//")) or not path.is_file():
        raise TranscriptionError("invalid_local_input")
    size = path.stat().st_size
    if size == 0:
        raise TranscriptionError("empty_audio")
    if size > max_bytes:
        raise TranscriptionError("audio_size_limit")
    return path


def normalize_window(raw: list[dict], start_sample: int, end_sample: int) -> list[dict]:
    """Keep VAD times inside the actual window, then shift by exact sample offset."""
    if (
        type(start_sample) is not int
        or type(end_sample) is not int
        or not 0 <= start_sample < end_sample
    ):
        raise TranscriptionError("invalid_worker_result", "window.interval")
    offset, duration = start_sample / 16000, (end_sample - start_sample) / 16000
    segments = []
    try:
        for entry in raw:
            for item in entry.get("sentence_info", []):
                text = re.sub(r"<\|[^|]*\|>", "", item["text"]).strip()
                if not text:
                    continue
                start, end = item["start"], item["end"]
                if (
                    type(start) not in (int, float)
                    or type(end) not in (int, float)
                    or not math.isfinite(start)
                    or not math.isfinite(end)
                ):
                    raise TranscriptionError("invalid_worker_result", "window.time_type")
                if not 0 <= start / 1000 < end / 1000 <= duration:
                    raise TranscriptionError("invalid_worker_result", "window.bounds")
                segments.append(
                    {"start": offset + start / 1000, "end": offset + end / 1000, "text": text}
                )
        if not segments and any(re.sub(r"<\|[^|]*\|>", "", x.get("text", "")).strip() for x in raw):
            raise TranscriptionError("invalid_worker_result", "window.missing_vad_segments")
    except (KeyError, TypeError, AttributeError):
        raise TranscriptionError("invalid_worker_result", "window.shape_or_type") from None
    return segments


def _check_chunks(data: dict, require) -> None:
    """Input coverage is geometric processing evidence, never speech accuracy."""
    if "chunk_manifest" not in data and "chunk_coverage" not in data:
        return
    chunks, coverage = data["chunk_manifest"], data["chunk_coverage"]
    total = round(data["duration"] * 16000)
    window = PARAMETERS["chunk_seconds"] * 16000
    count = (total + window - 1) // window
    require(isinstance(chunks, list) and len(chunks) == count, "chunk_manifest.coverage")
    position, empty = 0, 0
    for index, chunk in enumerate(chunks):
        start, end = index * window, min((index + 1) * window, total)
        require(
            chunk["index"] == index
            and chunk["start_sample"] == start
            and chunk["end_sample"] == end,
            "chunk_manifest.coverage",
        )
        require(
            chunk["start"] == start / 16000 and chunk["end"] == end / 16000,
            "chunk_manifest.time_origin",
        )
        require(re.fullmatch(r"[0-9a-f]{64}", chunk["asr_input_sha256"]), "chunk_manifest.hash")
        number = chunk["segment_count"]
        require(type(number) is int and number >= 0, "chunk_manifest.segment_count")
        require(
            chunk["status"] == ("speech_recognized" if number else "empty"), "chunk_manifest.status"
        )
        require(
            type(chunk["asr_seconds"]) in (int, float)
            and math.isfinite(chunk["asr_seconds"])
            and chunk["asr_seconds"] >= 0,
            "chunk_manifest.asr_seconds",
        )
        require(
            isinstance(chunk["diagnostic_file"], str) and chunk["diagnostic_file"],
            "chunk_manifest.diagnostic_file",
        )
        owned = data["segments"][position : position + number]
        require(
            len(owned) == number
            and all(chunk["start"] <= x["start"] < x["end"] <= chunk["end"] for x in owned),
            "chunk_manifest.segment_count",
        )
        position += number
        empty += not number
    require(position == len(data["segments"]), "chunk_manifest.segment_count")
    require(
        coverage
        == {
            "kind": "all_input_samples_processed",
            "total_sample_count": total,
            "chunk_count": count,
            "empty_chunk_count": empty,
        },
        "chunk_coverage",
    )


def validate_result(data: dict, expected_hash: str, max_duration: float) -> dict:
    if (
        isinstance(data, dict)
        and set(data) <= {"error", "rejected_field"}
        and isinstance(data["error"], str)
        and data["error"] in ERROR_CODES
    ):
        raise TranscriptionError(data["error"], data.get("rejected_field"))

    def require(condition, field):
        if not condition:
            raise TranscriptionError("invalid_worker_result", field)

    try:
        require(data["kind"] == "audio_transcription", "kind")
        require(data["audio_sha256"] == expected_hash, "audio_sha256")
        require(re.fullmatch(r"[0-9a-f]{64}", data["asr_input_sha256"]), "asr_input_sha256")
        require(data["asr_input_encoding"] == "pcm_f32le_16000_mono", "asr_input_encoding")
        require(
            data["asr_input_hash_basis"]
            in {
                "actual_worker_array",
                "reconstructed_from_pinned_decode",
            },
            "asr_input_hash_basis",
        )
        from . import onnx_profile

        onnx = data["models"] == onnx_profile.MODELS
        require(data["models"] == (onnx_profile.MODELS if onnx else MODELS), "models")
        require(data["software"] == (onnx_profile.SOFTWARE if onnx else SOFTWARE), "software")
        require(data["parameters"] == (onnx_profile.PARAMETERS if onnx else PARAMETERS), "parameters")
        require(data["timestamp_kind"] == (onnx_profile.TIMESTAMP_KIND if onnx else "vad_segment"),
                "timestamp_kind")
        require(data["time_origin"] == "local_audio_first_decoded_sample", "time_origin")
        require(data["automatically_verified_truth"] is False, "automatically_verified_truth")
        duration = data["duration"]
        require(type(duration) in (int, float) and math.isfinite(duration), "duration.type")
        require(0 < duration <= max_duration, "duration.bounds")
        segments = data["segments"]
        require(isinstance(segments, list) and segments, "segments")
        last_end = 0
        for index, segment in enumerate(segments):
            field = f"segments[{index}]"
            require(set(segment) == {"start", "end", "text"}, field + ".shape")
            start, end, text = segment["start"], segment["end"], segment["text"]
            require(type(start) in (int, float) and type(end) in (int, float), field + ".time_type")
            require(math.isfinite(start) and math.isfinite(end), field + ".time_finite")
            require(0 <= start < end <= duration, field + ".bounds")
            require(last_end <= start, field + ".overlap")
            require(isinstance(text, str) and text.strip() == text and text, field + ".text")
            require("<|" not in text, field + ".tags")
            last_end = end
        _check_chunks(data, require)
    except (KeyError, TypeError, ValueError):
        raise TranscriptionError("invalid_worker_result", "shape_or_type") from None
    return data


def _stop_worker(process: subprocess.Popen) -> None:
    """Bound cleanup of Python and its decoder, including on caller fence failures."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        process.kill()


def transcribe_local(
    audio_path: str | Path,
    *,
    python_path: Path = PILOT_ROOT / "venv/Scripts/python.exe",
    worker_script: Path | None = None,
    timeout_seconds: float = 900,
    max_duration: float = 3600,
    max_bytes: int = 512 * 1024 * 1024,
    cancel_check: Callable[[], bool] | None = None,
    on_poll: Callable[[], None] | None = None,
) -> dict:
    """One bounded subprocess; no retries, downloads, DB writes or truth claims."""
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or not 0 < max_duration <= 7200
        or max_bytes <= 0
    ):
        raise ValueError("invalid transcription limits")
    if cancel_check is not None and cancel_check():
        raise TranscriptionError("transcription_cancelled")
    path = local_audio_path(audio_path, max_bytes)
    before = audio_hash(path)
    script = worker_script or Path(__file__).resolve().parents[2] / "scripts/asr_transcribe.py"
    if not Path(python_path).is_file() or not script.is_file():
        raise TranscriptionError("local_runtime_unavailable")
    environment = os.environ.copy()
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HOME": str(PILOT_ROOT / "hf-cache"),
            "MODELSCOPE_CACHE": str(PILOT_ROOT / "models"),
            "NUMBA_CACHE_DIR": str(PILOT_ROOT / "cache/numba"),
            "TEMP": str(PILOT_ROOT / "tmp"),
            "TMP": str(PILOT_ROOT / "tmp"),
            "OMP_NUM_THREADS": "4",
            "PYTHONUTF8": "1",
        }
    )
    # No credentials are required by this local worker.
    for key in list(environment):
        if any(marker in key.upper() for marker in ("TOKEN", "API_KEY", "SECRET")):
            environment.pop(key)
    with tempfile.TemporaryDirectory(prefix="asr-") as directory:
        output = Path(directory) / "result.json"
        process = None
        try:
            process = subprocess.Popen(
                [
                    str(python_path),
                    str(script),
                    "--worker",
                    "--audio",
                    str(path),
                    "--output",
                    str(output),
                    "--max-duration",
                    str(max_duration),
                    "--expected-sha256",
                    before,
                ],
                env=environment,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                if cancel_check is None and on_poll is None:
                    process.wait(timeout=timeout_seconds)
                else:
                    deadline = time.monotonic() + timeout_seconds
                    while True:
                        if cancel_check is not None and cancel_check():
                            raise TranscriptionError("transcription_cancelled")
                        if on_poll is not None:
                            on_poll()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise subprocess.TimeoutExpired(process.args, timeout_seconds)
                        try:
                            process.wait(timeout=min(1.0, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            continue
                    if cancel_check is not None and cancel_check():
                        raise TranscriptionError("transcription_cancelled")
                    if on_poll is not None:
                        on_poll()
            except BaseException:
                _stop_worker(process)
                raise
        except subprocess.TimeoutExpired:
            raise TranscriptionError("transcription_timeout") from None
        except OSError:
            if process is not None:
                raise
            raise TranscriptionError("local_runtime_unavailable") from None
        if audio_hash(path) != before:
            raise TranscriptionError("input_changed")
        try:
            if output.stat().st_size > 16 * 1024 * 1024:
                raise TranscriptionError("invalid_worker_result")
            data = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise TranscriptionError("invalid_worker_result") from None
    result = validate_result(data, before, max_duration)
    if cancel_check is not None and cancel_check():
        raise TranscriptionError("transcription_cancelled")
    if on_poll is not None:
        on_poll()
    return result
