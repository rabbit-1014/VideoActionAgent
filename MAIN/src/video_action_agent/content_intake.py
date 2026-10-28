"""Operator-run source-only candidate intake; no product task or downstream generation."""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from pathlib import Path
from uuid import uuid4

from video_action_agent.audio_session import AudioSession, AudioSessionError
from video_action_agent.local_transcriber import (
    TranscriptionError,
    audio_hash,
    local_audio_path,
)
from video_action_agent.onnx_transcriber import transcribe_onnx as transcribe_local
from video_action_agent.source_access import SourceAccessError, VideoRef

INTAKE_ROOT = Path("D:/VideoActionAgent-asr-pilot/source-intake")
MAX_ENVELOPE = 16 * 1024 * 1024


class IntakeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _build_bundle(envelope, ref, acquisition, audio_path):
    from video_action_agent.source_candidate_bundle import build_bundle

    return build_bundle(envelope, ref, acquisition, audio_path)


def _save_bundle(bundle, output_dir):
    from video_action_agent.source_candidate_bundle import save_bundle

    return save_bundle(bundle, output_dir)


def _read_bundle(output_dir):
    from video_action_agent.source_candidate_bundle import read_bundle

    return read_bundle(output_dir)


def _stable_error(exc):
    code = getattr(exc, "code", "intake_failed")
    if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        code = "intake_failed"
    return IntakeError(code)


def _ref(source):
    try:
        return VideoRef.parse(source)
    except SourceAccessError:
        raise IntakeError("invalid_source") from None


def _local_path(value):
    raw = str(value)
    if "://" in raw or raw.startswith(("\\\\", "//")):
        raise IntakeError("invalid_local_path")
    path = Path(value).resolve()
    if str(path).startswith(("\\\\", "//")):
        raise IntakeError("invalid_local_path")
    return path


def _output(value):
    output = _local_path(value)
    if output.exists():
        raise IntakeError("output_exists")
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        # Check local publication parent before starting any remote acquisition.
        with tempfile.TemporaryFile(dir=output.parent):
            pass
    except OSError:
        raise IntakeError("output_unavailable") from None
    return output


def _identity(proof, ref):
    if (
        not isinstance(proof, dict) or proof.get("bvid") != ref.bvid
        or type(proof.get("page")) is not int or proof["page"] != ref.page
        or type(proof.get("cid")) is not int or proof["cid"] < 1
        or not isinstance(proof.get("part_title"), str) or not proof["part_title"].strip()
    ):
        raise IntakeError("source_mismatch")
    duration = proof.get("duration_seconds")
    if type(duration) not in (float, int) or not math.isfinite(duration) or duration <= 0:
        raise IntakeError("invalid_duration")
    if duration > 7200:
        raise IntakeError("duration_limit")


def _verify_audio(envelope, ref, path):
    if not isinstance(envelope, dict):
        raise IntakeError("invalid_envelope")
    _identity(envelope.get("source_evidence"), ref)
    transcription = envelope.get("transcription")
    if not isinstance(transcription, dict):
        raise IntakeError("invalid_envelope")
    actual = audio_hash(path)
    if transcription.get("audio_sha256") != actual:
        raise IntakeError("audio_integrity_mismatch")


def _summary(saved):
    return {
        "state": "unverified_source_candidate", "completed": False, "formal_task": False,
        "acquisition": saved["acquisition"], "segments": saved["segments"],
        "paths": saved["paths"],
    }


def capture(source, output_dir, *, allow_local_audio_read=False, on_poll=None, cancel_check=None,
            on_stage=None):
    if not allow_local_audio_read:
        raise IntakeError("audio_permission_required")
    ref = _ref(source)
    output = _output(output_dir)
    session = None
    def checkpoint(stage):
        if cancel_check is not None and cancel_check():
            raise IntakeError("cancelled")
        if on_stage is not None:
            on_stage(stage)

    try:
        checkpoint("resolving")
        session = AudioSession(ref, on_poll=on_poll, cancel_check=cancel_check)
        proof = session.resolve()
        _identity(proof, ref)
        destination = INTAKE_ROOT / uuid4().hex / "audio.m4a"
        checkpoint("downloading")
        downloaded = session.download(destination)
        _identity(downloaded, ref)
        if downloaded["cid"] != proof["cid"]:
            raise IntakeError("source_mismatch")
        checkpoint("transcribing")
        transcription = transcribe_local(
            destination, timeout_seconds=900, max_duration=7200,
            on_poll=on_poll, cancel_check=cancel_check,
        )
        envelope = {"schema_version": 1, "source_evidence": proof,
                    "transcription": transcription}
        _verify_audio(envelope, ref, destination)
        bundle = _build_bundle(envelope, ref, "live_anonymous_audio", destination)
        checkpoint("saving")
        return _summary(_save_bundle(bundle, output))
    except (AudioSessionError, TranscriptionError) as exc:
        raise _stable_error(exc) from None
    except IntakeError:
        raise
    except Exception as exc:  # noqa: BLE001 -- safe codes across optional bundle boundary
        raise _stable_error(exc) from None
    finally:
        if session is not None:
            session.close()


def reuse(source, envelope_path, audio_file, output_dir):
    ref = _ref(source)
    output = _output(output_dir)
    try:
        path = _local_path(envelope_path)
        if not path.is_file() or path.stat().st_size > MAX_ENVELOPE:
            raise IntakeError("invalid_envelope")
        envelope = json.loads(path.read_text(encoding="utf-8-sig"))
        audio = local_audio_path(_local_path(audio_file), 512 * 1024 * 1024)
        _verify_audio(envelope, ref, audio)
        bundle = _build_bundle(envelope, ref, "recorded_audio", audio)
        return _summary(_save_bundle(bundle, output))
    except IntakeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise IntakeError("invalid_envelope") from None
    except Exception as exc:  # noqa: BLE001 -- bundle validation emits stable diagnostic codes
        raise _stable_error(exc) from None


def read(bundle_dir):
    directory = _local_path(bundle_dir)
    try:
        bundle = _read_bundle(directory)
        envelope = bundle["source_envelope"]
        segments = len(envelope["transcription"]["segments"])
        return _summary({"acquisition": bundle["acquisition"], "segments": segments,
                         "paths": {name: str(directory / filename) for name, filename in (
                             ("json", "candidate.json"), ("markdown", "transcript.md"),
                             ("srt", "transcript.srt"), ("manifest", "manifest.json"))}})
    except Exception as exc:  # noqa: BLE001 -- never dump full candidate on validation failure
        raise _stable_error(exc) from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    capture_parser = subparsers.add_parser("capture")
    capture_parser.add_argument("--source", required=True)
    capture_parser.add_argument("--allow-local-audio-read", required=True, action="store_true")
    capture_parser.add_argument("--output-dir", required=True)
    reuse_parser = subparsers.add_parser("reuse")
    reuse_parser.add_argument("--source", required=True)
    reuse_parser.add_argument("--envelope", required=True)
    reuse_parser.add_argument("--audio-file", required=True)
    reuse_parser.add_argument("--output-dir", required=True)
    read_parser = subparsers.add_parser("read")
    read_parser.add_argument("--bundle-dir", required=True)
    args = parser.parse_args(argv)
    # Input reference is an argument error, before external SDK/ASR calls.
    try:
        if args.mode in ("capture", "reuse"):
            _ref(args.source)
            _local_path(args.output_dir)
        if args.mode == "reuse":
            _local_path(args.envelope)
            _local_path(args.audio_file)
        if args.mode == "read":
            _local_path(args.bundle_dir)
    except IntakeError as exc:
        parser.error(exc.code)
    try:
        if args.mode == "capture":
            summary = capture(args.source, args.output_dir, allow_local_audio_read=True)
        elif args.mode == "reuse":
            summary = reuse(args.source, args.envelope, args.audio_file, args.output_dir)
        else:
            summary = read(args.bundle_dir)
        print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({"error_code": "cancelled"}))
        return 130
    except IntakeError as exc:
        print(json.dumps({"error_code": exc.code}))
        return 1

