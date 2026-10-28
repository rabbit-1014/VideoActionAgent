"""Reuse immutable ASR only after matching freshly downloaded audio bytes."""
from datetime import datetime, timezone
from pathlib import Path

from bridge_contract import atomic, digest
from video_action_agent.audio_session import AudioSession
from video_action_agent.content_intake import IntakeError, capture
from video_action_agent.source_access import VideoRef
from video_action_agent.source_candidate_bundle import (
    build_bundle,
    read_bundle,
    save_bundle,
)

CACHES = [
    Path("D:/VideoActionAgent-asr-pilot/source-bundles/BV1AJen6SEVQ-p42-20260927-v1"),
    Path("D:/CodexWork/VideoActionAgent-link-native-20261014/runtime/sources/ba190ae4-7050-5a30-af9a-d4ca065fadb1"),
]


class VerifiedCacheCapture:
    def __init__(self, root, *, caches=CACHES, bundle_supplier=None,
                 session_factory=AudioSession, fallback=capture):
        self.root, self.caches = Path(root), caches
        # Caller supplies bounded, owner-validated bundles, never user-provided paths.
        self.bundle_supplier = bundle_supplier
        self.session_factory, self.fallback = session_factory, fallback

    def __call__(self, source, output, *, allow_local_audio_read=False, cancel_check=None, on_stage=None):
        if allow_local_audio_read is not True:
            raise IntakeError("audio_permission_required")
        ref = VideoRef.parse(source)
        def checkpoint():
            if cancel_check and cancel_check():
                raise IntakeError("cancelled")
        def stage(value):
            checkpoint()
            if on_stage:
                on_stage(value)
        checkpoint()
        candidates = []
        if self.bundle_supplier is not None:
            for bundle in self.bundle_supplier(ref):
                checkpoint()
                proof = bundle["source_envelope"]["source_evidence"]
                if (proof["bvid"], proof["page"]) == (ref.bvid, ref.page):
                    candidates.append(bundle)
        if not candidates:
            for path in self.caches:
                checkpoint()
                bundle = read_bundle(path)
                proof = bundle["source_envelope"]["source_evidence"]
                if (proof["bvid"], proof["page"]) == (ref.bvid, ref.page):
                    candidates.append(bundle)
                    break  # Preserve legacy path selection when no supplier is used.
        checkpoint()
        if not candidates:
            return self.fallback(source, output, allow_local_audio_read=True,
                                 cancel_check=cancel_check, on_stage=on_stage)
        work = self.root / Path(output).name
        work.mkdir(parents=True, exist_ok=True)
        intent = work / "attempt.json"
        if intent.exists():
            raise IntakeError("intake_failed")
        atomic(intent, {"state": "resolving", "source": {"bvid": ref.bvid, "page": ref.page},
                        "automatic_retry": False})
        session = self.session_factory(ref, on_poll=checkpoint, cancel_check=cancel_check)
        try:
            stage("resolving")
            fresh_proof = session.resolve()
            stage("downloading")
            current = session.download(work / "current-audio.m4a")
            checkpoint()
            if current["cid"] != fresh_proof["cid"]:
                raise IntakeError("source_mismatch")
        finally:
            session.close()
        matched = None
        for bundle in candidates:
            old = bundle["source_envelope"]["source_evidence"]
            if (all(current[key] == old[key] for key in ("bvid", "page", "cid"))
                    and current["audio_sha256"] == bundle["source_envelope"]["transcription"]["audio_sha256"]
                    and abs(current["stream_duration_seconds"] - old["stream_duration_seconds"]) < .05):
                matched = bundle
                break
        equal = matched is not None
        atomic(intent, {"state": "matched_saved_asr" if equal else "different_audio",
                        "fresh_audio": current, "saved_bundle_sha256": digest(matched or candidates[0]),
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "new_asr_calls": 0 if equal else None, "automatic_retry": False})
        if not equal:
            # This completed comparison is known, not an unknown-operation retry.
            return self.fallback(source, output, allow_local_audio_read=True,
                                 cancel_check=cancel_check, on_stage=on_stage)
        stage("saving")
        envelope = {"schema_version": 1, "source_evidence": fresh_proof,
                    "transcription": matched["source_envelope"]["transcription"]}
        verified = build_bundle(envelope, ref, "live_anonymous_audio", work / "current-audio.m4a")
        save_bundle(verified, Path(output))
        atomic(Path(output) / "current-audio-reuse-proof.json", {
            "kind": "fresh_audio_hash_matched_saved_asr_not_new_transcription",
            "fresh_audio": current, "saved_bundle_sha256": digest(matched),
            "observed_at": datetime.now(timezone.utc).isoformat(), "new_asr_calls": 0})
        write_reuse_presentation(Path(output))


def write_reuse_presentation(output):
    """Separate effective provenance from the upstream renderer's legacy generic header."""
    output = Path(output)
    if not (output / "current-audio-reuse-proof.json").is_file():
        raise IntakeError("source_bundle_unavailable")
    (output / "SOURCE-PROVENANCE.md").write_text(
        "# 本次来源读取说明\n\n本次重新匿名读取了此分P的音轨；CID、时长、音轨SHA256与已保存转写输入一致。"
        "转写正文因此复用已存ASR，本次没有重新运行语音识别。\n\n"
        "current-audio-reuse-proof.json 保存当前读取与复用依据。candidate/manifest/transcript是兼容旧格式的包；"
        "transcript.md的通用模板开头‘本次…在本机转写’不能用来证明本次运行过ASR，原字节保留。\n",
        encoding="utf-8")
