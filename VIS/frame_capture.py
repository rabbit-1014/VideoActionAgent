"""Source-bound, one-shot frame acquisition for a validated source envelope."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

WORKER_PYTHON = Path("D:/VideoActionAgent-asr-pilot/audio-venv/Scripts/python.exe")
WORKER_SCRIPT = Path(__file__).with_name("frame_worker.py")
MAX_FRAMES = 3
WORKER_TIMEOUT_SECONDS = 180.0
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FrameCaptureError(ValueError):
    """Stable safe error; never includes worker output or a signed URL."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _source(material: dict) -> tuple[dict, float]:
    if not isinstance(material, dict):
        raise FrameCaptureError("invalid_material")
    evidence = material.get("source_evidence")
    if not isinstance(evidence, dict):
        raise FrameCaptureError("invalid_material")
    bvid, page, cid = evidence.get("bvid"), evidence.get("page"), evidence.get("cid")
    duration = evidence.get("duration_seconds")
    if not isinstance(bvid, str) or not _BVID_RE.fullmatch(bvid):
        raise FrameCaptureError("invalid_source")
    if type(page) is not int or page < 1 or type(cid) is not int or cid < 1:
        raise FrameCaptureError("invalid_source")
    if type(duration) not in (float, int) or not math.isfinite(duration) or duration <= 0:
        raise FrameCaptureError("invalid_duration")
    return {"bvid": bvid, "page": page, "cid": cid}, float(duration)


def _requested_times(times: list[float], duration: float) -> list[float]:
    if not isinstance(times, list) or not 1 <= len(times) <= MAX_FRAMES:
        raise FrameCaptureError("invalid_frame_count")
    result: list[float] = []
    for at in times:
        if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= at < duration:
            raise FrameCaptureError("invalid_frame_time")
        result.append(float(at))
    if len(set(result)) != len(result):
        raise FrameCaptureError("duplicate_frame_time")
    return result


def _read_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: dict, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "x" if exclusive else "w"
    with path.open(mode, encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def _frame_valid(frame: object, index: int, requested: float, duration: float) -> bool:
    if not isinstance(frame, dict):
        return False
    pts = frame.get("actual_frame_pts_seconds")
    return (
        frame.get("id") == f"f{index:03d}"
        and frame.get("requested_seconds") == requested
        and frame.get("path") == f"frames/f{index:03d}.jpg"
        and type(pts) in (float, int)
        and math.isfinite(pts)
        and 0 <= pts < duration
        and abs(pts - requested) <= 1.0
        and type(frame.get("width")) is int
        and frame["width"] > 0
        and type(frame.get("height")) is int
        and frame["height"] > 0
        and type(frame.get("bytes")) is int
        and frame["bytes"] > 0
        and isinstance(frame.get("sha256"), str)
        and _SHA256_RE.fullmatch(frame["sha256"]) is not None
    )


def _verify_cached(directory: Path, identity: dict, times: list[float], duration: float) -> dict:
    intent = _read_json(directory / "source-intent.json")
    result = _read_json(directory / "source-result.json")
    proof = _read_json(directory / "source-proof.json")
    expected_scope = {
        "source": identity,
        "requested_seconds": times,
        "duration_seconds": duration,
    }
    if not intent or any(intent.get(key) != value for key, value in expected_scope.items()):
        raise FrameCaptureError("output_identity_mismatch")
    if not result or result.get("state") != "available" or not proof:
        raise FrameCaptureError("prior_attempt_incomplete")
    if result.get("source") != identity or result.get("requested_seconds") != times:
        raise FrameCaptureError("output_identity_mismatch")
    if (
        proof.get("source") != identity
        or proof.get("duration_seconds") != duration
        or result.get("automatic_retry") is not False
    ):
        raise FrameCaptureError("stored_result_invalid")
    frames = result.get("frames")
    if not isinstance(frames, list) or len(frames) != len(times):
        raise FrameCaptureError("stored_result_invalid")
    for index, frame in enumerate(frames):
        if not _frame_valid(frame, index, times[index], duration):
            raise FrameCaptureError("stored_result_invalid")
        image_path = directory / frame["path"]
        try:
            data = image_path.read_bytes()
        except OSError:
            raise FrameCaptureError("stored_frame_missing") from None
        if hashlib.sha256(data).hexdigest() != frame["sha256"] or len(data) != frame["bytes"]:
            raise FrameCaptureError("stored_frame_hash_mismatch")
    return result


def _stop_worker_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _launch_worker(payload: dict, checkpoint: Callable[[], object]) -> dict:
    if not WORKER_PYTHON.is_file() or not WORKER_SCRIPT.is_file():
        raise FrameCaptureError("worker_unavailable")
    try:
        process = subprocess.Popen(
            [str(WORKER_PYTHON), str(WORKER_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise FrameCaptureError("worker_start_failed") from None
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    try:
        if process.stdin is None:
            raise FrameCaptureError("worker_start_failed")
        process.stdin.write(encoded)
        process.stdin.close()
        started = time.monotonic()
        while process.poll() is None:
            checkpoint()
            if time.monotonic() - started >= WORKER_TIMEOUT_SECONDS:
                raise FrameCaptureError("worker_timeout")
            time.sleep(0.2)
        stdout = process.stdout.read() if process.stdout else b""
    except BaseException:
        _stop_worker_tree(process)
        raise
    if process.returncode != 0:
        raise FrameCaptureError("worker_failed")
    try:
        response = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise FrameCaptureError("worker_result_invalid") from None
    if not isinstance(response, dict):
        raise FrameCaptureError("worker_result_invalid")
    return response


def _validate_worker_result(
    result: dict, identity: dict, times: list[float], duration: float, directory: Path
) -> dict:
    proof = result.get("proof")
    if (
        result.get("schema_version") != 1
        or result.get("state") != "available"
        or result.get("source") != identity
        or result.get("automatic_retry") is not False
        or not isinstance(proof, dict)
        or proof.get("source") != identity
        or proof.get("duration_seconds") != duration
    ):
        raise FrameCaptureError("worker_identity_mismatch")
    frames = result.get("frames")
    if not isinstance(frames, list) or len(frames) != len(times):
        raise FrameCaptureError("worker_result_invalid")
    for index, frame in enumerate(frames):
        if not _frame_valid(frame, index, times[index], duration):
            raise FrameCaptureError("worker_result_invalid")
        try:
            data = (directory / frame["path"]).read_bytes()
        except OSError:
            raise FrameCaptureError("worker_frame_missing") from None
        if hashlib.sha256(data).hexdigest() != frame["sha256"] or len(data) != frame["bytes"]:
            raise FrameCaptureError("worker_frame_hash_mismatch")
    calls = result.get("calls")
    if (
        not isinstance(calls, dict)
        or calls.get("info") != 1
        or calls.get("playurl") != 1
        or calls.get("ffmpeg_attempts") != len(times)
    ):
        raise FrameCaptureError("worker_call_count_invalid")
    return proof


def capture_frames(
    material: dict,
    times: list[float],
    output_dir: Path,
    *,
    checkpoint: Callable[[], object],
) -> dict:
    """Capture at most three exact-source frames; this call never auto-retries."""
    identity, duration = _source(material)
    requested = _requested_times(times, duration)
    directory = Path(output_dir).resolve()
    if directory.exists():
        if any(directory.iterdir()):
            return _verify_cached(directory, identity, requested, duration)
    else:
        directory.mkdir(parents=True, exist_ok=False)
    intent = {
        "schema_version": 1,
        "state": "intent",
        "source": identity,
        "requested_seconds": requested,
        "duration_seconds": duration,
        "automatic_retry": False,
        "started_at": datetime.now(UTC).isoformat(),
    }
    try:
        _write_json(directory / "source-intent.json", intent, exclusive=True)
    except FileExistsError:
        return _verify_cached(directory, identity, requested, duration)
    safe_failure = {
        "schema_version": 1,
        "state": "failed",
        "source": identity,
        "requested_seconds": requested,
        "calls": {"info": 0, "playurl": 0, "ffmpeg_attempts": 0},
        "automatic_retry": False,
    }
    try:
        checkpoint()
        result = _launch_worker(
            {
                "source": identity,
                "duration_seconds": duration,
                "requested_seconds": requested,
                "output_dir": str(directory),
            },
            checkpoint,
        )
        if result.get("state") != "available":
            calls = result.get("calls")
            if isinstance(calls, dict):
                safe_failure["calls"] = {
                    key: calls.get(key, 0)
                    for key in ("info", "playurl", "ffmpeg_attempts")
                }
            code = result.get("error_code")
            safe_failure["error_code"] = code if isinstance(code, str) else "worker_failed"
            _write_json(directory / "source-result.json", safe_failure)
            return safe_failure
        proof = _validate_worker_result(result, identity, requested, duration, directory)
        public_result = {
            "schema_version": 1,
            "state": "available",
            "source": identity,
            "requested_seconds": requested,
            "observed_at": proof["observed_at"],
            "frames": result["frames"],
            "calls": result["calls"],
            "automatic_retry": False,
        }
        _write_json(directory / "source-proof.json", proof)
        _write_json(directory / "source-result.json", public_result)
        return public_result
    except FrameCaptureError as exc:
        safe_failure["error_code"] = exc.code
        _write_json(directory / "source-result.json", safe_failure)
        raise
    except Exception:  # noqa: BLE001 - redact unknown SDK/worker failures at the boundary.
        safe_failure["error_code"] = "capture_failed"
        _write_json(directory / "source-result.json", safe_failure)
        raise FrameCaptureError("capture_failed") from None
