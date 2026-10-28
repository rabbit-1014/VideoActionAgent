"""Read a trusted, pinned research bundle; not a live acquisition or evidence adjudicator.

Only raw materials and ASR segments cross this adapter. Manual annotations,
historical web conclusions and the old generated answer never do.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Literal


class SourceInputError(ValueError):
    """A stable diagnostic code, without leaking local paths or source content."""


@dataclass(frozen=True)
class Material:
    sha256: str
    integrity_basis: Literal["recorded_digest", "computed_at_import"]
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Frame:
    timestamp_seconds: float
    material: Material


@dataclass(frozen=True)
class RecordedSource:
    bvid: str
    title: str
    duration_seconds: float
    manifest_generated_at: str
    audio: Material
    transcript: Material
    frame_manifest: Material
    evidence_manifest_sha256: str
    segments: tuple[Segment, ...]
    frames: tuple[Frame, ...]
    asr_model: str
    limitations: tuple[str, ...]
    mode: Literal["recorded_fixture"] = "recorded_fixture"


def _path(root: Path, relative: str) -> Path:
    if PureWindowsPath(relative).drive or Path(relative).is_absolute():
        raise SourceInputError("path_outside_bundle")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise SourceInputError("path_outside_bundle")
    return target


def _material(path: Path, expected: str | None = None) -> Material:
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise SourceInputError("material_missing") from exc
    actual = hashlib.sha256(content).hexdigest()
    if expected is not None and actual != expected.lower():
        raise SourceInputError("integrity_mismatch")
    return Material(actual, "recorded_digest" if expected else "computed_at_import", content)


def _json(material: Material) -> dict:
    try:
        value = json.loads(material.content)
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        raise SourceInputError("invalid_json") from exc


def _number(value, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceInputError(code)
    if not math.isfinite(value) or value < 0:
        raise SourceInputError(code)
    return float(value)


def _text(value, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceInputError(code)
    return value


def _segments(data: dict, bvid: str, duration: float) -> tuple[Segment, ...]:
    code = "invalid_transcript"
    try:
        if data["source"]["bvid"] != bvid or data["source"]["duration_seconds"] != duration:
            raise SourceInputError(code)
        rows = data["segments"]
        if not rows or data["asr"]["segment_count"] != len(rows):
            raise SourceInputError(code)
        result = []
        previous_start = -1
        for row in rows:
            start, end = _number(row["start"], code), _number(row["end"], code)
            if start >= end or end > duration or start < previous_start:
                raise SourceInputError(code)
            result.append(Segment(start, end, _text(row["text"], code)))
            previous_start = start
        return tuple(result)
    except (KeyError, TypeError) as exc:
        raise SourceInputError(code) from exc


def load_recorded_source(root: Path, evidence_path: str, expected_sha256: str) -> RecordedSource:
    """Caller supplies the trusted manifest pin, never a pin from user-submitted data.

    Reads a bounded local research fixture into immutable bytes. This adapter
    is not exposed to the public API and is unsuitable for unbounded video files.
    Hashes identify bytes, not their truth or the user's permission to access them.
    """
    root = root.resolve()
    try:
        evidence_material = _material(_path(root, evidence_path), expected_sha256)
        evidence = _json(evidence_material)
        if evidence["schema_version"] != 1:
            raise SourceInputError("unsupported_schema")
        source = evidence["source"]
        bvid = _text(source["bvid"], "invalid_source")
        duration = _number(source["duration_seconds"], "invalid_source")
        if not duration:
            raise SourceInputError("invalid_source")
        audio_path = _path(root, source["audio"]["path"])
        audio = _material(audio_path, source["audio"]["sha256"])
        extraction = evidence["extraction"]
        asr = extraction["local_asr"]
        transcript = _material(_path(root, asr["path"]), asr["sha256"])
        transcript_data = _json(transcript)
        segments = _segments(transcript_data, bvid, duration)
        frame_info = extraction["target_frames"]
        frame_manifest = _material(
            _path(root, frame_info["manifest_path"]), frame_info["manifest_sha256"]
        )
        frame_data = _json(frame_manifest)
        if frame_data["bvid"] != bvid or frame_data["duration_seconds"] != duration:
            raise SourceInputError("invalid_frames")
        # Legacy frame paths are absolute. Rebase only paths inside the recorded
        # audio directory, never by basename (which could silently substitute files).
        recorded_dir = PureWindowsPath(transcript_data["source"]["audio"]).parent
        frames = []
        for row in frame_data["frames"]:
            timestamp = _number(row["timestamp_seconds"], "invalid_frames")
            if row["success"] is not True or timestamp > duration:
                raise SourceInputError("invalid_frames")
            try:
                relative = PureWindowsPath(row["path"]).relative_to(recorded_dir).as_posix()
            except ValueError as exc:
                raise SourceInputError("path_outside_bundle") from exc
            material = _material(_path(audio_path.parent, relative))
            if len(material.content) != row["bytes"]:
                raise SourceInputError("frame_size_mismatch")
            frames.append(Frame(timestamp, material))
        return RecordedSource(
            bvid=bvid,
            title=_text(source["title"], "invalid_source"),
            duration_seconds=duration,
            manifest_generated_at=_text(evidence["generated_at"], "invalid_source"),
            audio=audio,
            transcript=transcript,
            frame_manifest=frame_manifest,
            evidence_manifest_sha256=evidence_material.sha256,
            segments=segments,
            frames=tuple(frames),
            asr_model=_text(asr["model"], "invalid_source"),
            limitations=tuple(asr.get("limitations", []))
            + (
                "Recorded fixture, not live acquisition.",
                "ASR text is uncorrected and not independently verified.",
                "Frames are sparse samples without automated visual interpretation.",
                "Legacy frames have no recorded digest; hashes identify import-time bytes only.",
                "Manual claims and historical external-source conclusions are excluded.",
            ),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise SourceInputError("invalid_bundle") from exc
