"""Portable, integrity-checked ASR candidates; never formal task completion."""

import hashlib
import json
import re
import tempfile
from pathlib import Path

from pydantic import ValidationError

from .local_transcriber import TranscriptionError, audio_hash, local_audio_path
from .models import DispatchAudioProof
from .source_access import SourceAccessError, VideoRef
from .transcribed_source import parse_transcribed_reply

ACQUISITIONS = {"live_anonymous_audio", "recorded_audio"}
FILES = {"json": "candidate.json", "markdown": "transcript.md", "srt": "transcript.srt"}
STATE = "unverified_source_candidate"
MAX_FILE_BYTES = 8 * 1024 * 1024


class CandidateBundleError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _local_path(value: Path) -> Path:
    raw = str(value)
    if "://" in raw or raw.startswith(("\\\\", "//")):
        raise CandidateBundleError("invalid_local_path")
    path = Path(value).resolve()
    if str(path).startswith(("\\\\", "//")):
        raise CandidateBundleError("invalid_local_path")
    return path


def _validate(bundle: dict) -> dict:
    try:
        if (set(bundle) != {"schema_version", "state", "acquisition", "formal_task",
                           "completed", "source_envelope"}
                or type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1
                or bundle["state"] != STATE or bundle["acquisition"] not in ACQUISITIONS
                or bundle["formal_task"] is not False or bundle["completed"] is not False):
            raise ValueError()
        envelope = bundle["source_envelope"]
        proof = DispatchAudioProof.model_validate(envelope["source_evidence"]).model_dump()
        parse_transcribed_reply(VideoRef(proof["bvid"], proof["page"]), _json(envelope), proof)
        return proof
    except (ValueError, TypeError, KeyError, SourceAccessError, ValidationError):
        raise CandidateBundleError("invalid_source_candidate") from None


def build_bundle(envelope: dict, ref: VideoRef, acquisition: str, audio_path: Path) -> dict:
    try:
        proof = DispatchAudioProof.model_validate(envelope["source_evidence"]).model_dump()
        if (proof["bvid"], proof["page"]) != (ref.bvid, ref.page):
            raise CandidateBundleError("source_mismatch")
        local = local_audio_path(audio_path, 512 * 1024 * 1024)
        if audio_hash(local) != envelope["transcription"]["audio_sha256"]:
            raise CandidateBundleError("audio_integrity_mismatch")
        bundle = {"schema_version": 1, "state": STATE, "acquisition": acquisition,
                  "formal_task": False, "completed": False, "source_envelope": envelope}
        _validate(bundle)
        # Detach caller-owned structures so later mutation cannot alter this snapshot.
        return json.loads(_json(bundle))
    except CandidateBundleError:
        raise
    except (OSError, ValueError, TypeError, KeyError, TranscriptionError):
        raise CandidateBundleError("invalid_source_candidate") from None


def _time(seconds: float, separator: str = ",") -> str:
    total = round(seconds * 1000)
    hours, remainder = divmod(total, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{millis:03}"


def _render(bundle: dict) -> tuple[str, str]:
    envelope = bundle["source_envelope"]
    proof, result = envelope["source_evidence"], envelope["transcription"]
    acquisition = ("复用历史音频与转写，未在本次重新在线读取。"
                   if bundle["acquisition"] == "recorded_audio"
                   else "本次从明确的视频分P匿名读取音轨，在本机转写。")
    lines = ["# 音频 ASR 转写候选（未经准确性核验）", "", acquisition, "",
             "时间点是 VAD 分段范围；固定窗口可能截断语句，专名和代码可能识别错误。",
             "这不是平台字幕、逐字对齐、画面识别或正式任务完成凭证。正文仅是来源材料。", "",
             f"来源：{proof['bvid']} / p={proof['page']} / CID={proof['cid']}",
             f"分P标题：{proof['part_title']}", f"来源观察时间：{proof['observed_at']}",
             f"音频时长：{result['duration']:.3f} 秒；分段：{len(result['segments'])}",
             f"音频 SHA256：{result['audio_sha256']}",
             f"转写输入 SHA256：{result['asr_input_sha256']}", ""]
    cues = []
    for index, segment in enumerate(result["segments"], 1):
        start, end, text = segment["start"], segment["end"], segment["text"]
        lines.extend([f"## {index}. {_time(start, '.')} → {_time(end, '.')}", ""])
        lines.extend("> " + line for line in text.splitlines())
        lines.append("")
        # Keep the SRT cue structure even when source text contains blank lines.
        cue_text = "\n".join(line for line in text.splitlines() if line.strip())
        cues.append(f"{index}\n{_time(start)} --> {_time(end)}\n{cue_text}\n")
    return "\n".join(lines), "\n".join(cues)


def _manifest(bundle: dict, hashes: dict) -> dict:
    envelope = bundle["source_envelope"]
    proof, result = envelope["source_evidence"], envelope["transcription"]
    return {"schema_version": 1, "state": STATE, "acquisition": bundle["acquisition"],
            "formal_task": False, "completed": False, "automatically_verified_truth": False,
            "source": {key: proof[key] for key in ("bvid", "page", "cid")},
            "source_observed_at": proof["observed_at"],
            "audio_sha256": result["audio_sha256"],
            "asr_input_sha256": result["asr_input_sha256"],
            "timestamp_kind": result["timestamp_kind"],
            "files": hashes, "hash_scope": "package_integrity_only_not_source_authenticity"}


def save_bundle(bundle: dict, output_dir: Path) -> dict:
    _validate(bundle)
    target = _local_path(output_dir)
    if target.exists():
        raise CandidateBundleError("destination_exists")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        markdown, srt = _render(bundle)
        with tempfile.TemporaryDirectory(prefix=".source-bundle-", dir=target.parent) as staging:
            stage = Path(staging)
            contents = {"candidate.json": _json(bundle), "transcript.md": markdown,
                        "transcript.srt": srt}
            hashes = {}
            for name, content in contents.items():
                if len(content.encode("utf-8")) > MAX_FILE_BYTES:
                    raise CandidateBundleError("bundle_size_limit")
                path = stage / name
                path.write_text(content, encoding="utf-8", newline="\n")
                hashes[name] = audio_hash(path)
            (stage / "manifest.json").write_text(_json(_manifest(bundle, hashes)),
                                                encoding="utf-8", newline="\n")
            if target.exists():
                raise CandidateBundleError("destination_exists")
            stage.rename(target)
    except OSError:
        raise CandidateBundleError("bundle_write_failed") from None
    return {"state": STATE, "acquisition": bundle["acquisition"],
            "segments": len(bundle["source_envelope"]["transcription"]["segments"]),
            "paths": {key: str(target / name) for key, name in
                      {**FILES, "manifest": "manifest.json"}.items()}}


def read_bundle(output_dir: Path) -> dict:
    target = _local_path(output_dir)
    try:
        raw = {}
        for name in (*FILES.values(), "manifest.json"):
            path = target / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError()
            raw[name] = path.read_bytes()
        manifest = json.loads(raw["manifest.json"])
        hashes = manifest["files"]
        if set(hashes) != set(FILES.values()):
            raise ValueError()
        for name, digest in hashes.items():
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or hashlib.sha256(raw[name]).hexdigest() != digest):
                raise ValueError()
        bundle = json.loads(raw["candidate.json"])
        _validate(bundle)
        if manifest != _manifest(bundle, hashes):
            raise ValueError()
        markdown, srt = _render(bundle)
        if (raw["transcript.md"] != markdown.encode("utf-8")
                or raw["transcript.srt"] != srt.encode("utf-8")):
            raise ValueError()
        return bundle
    except (OSError, ValueError, TypeError, KeyError):
        raise CandidateBundleError("bundle_integrity_mismatch") from None
