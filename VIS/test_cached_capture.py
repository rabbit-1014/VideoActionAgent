"""Known matching audio can reuse ASR without pretending to run it again."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "C:/Users/泽/Documents/VideoActionAgent/src")
sys.path.insert(0, "D:/CodexWork/VideoActionAgent-downstream-20261014")
from video_action_agent.source_candidate_bundle import read_bundle

from cached_capture import CACHES, VerifiedCacheCapture


class CachedCaptureTest(unittest.TestCase):
    def _supplied_capture(self, root, bundles, current, *, fallback):
        proof = copy.deepcopy(current["source_envelope"]["source_evidence"])
        proof["observed_at"] = "2026-10-28T00:00:00+08:00"
        audio_sha = current["source_envelope"]["transcription"]["audio_sha256"]
        calls = []
        class Session:
            def __init__(self, *args, **kwargs):
                pass
            def resolve(self):
                calls.append("resolve")
                return proof
            def download(self, path):
                calls.append("download")
                return {**proof, "audio_sha256": audio_sha, "local_path": str(path)}
            def close(self):
                pass
        def supplier(ref):
            self.assertEqual((ref.bvid, ref.page), (proof["bvid"], proof["page"]))
            calls.append("supplier")
            return bundles
        def build(envelope, ref, acquisition, audio_path):
            # The session is fake; retain real bundle validation/save/read, but no audio I/O.
            value = copy.deepcopy(current)
            value.update(source_envelope=envelope, acquisition=acquisition)
            return value
        try:
            fn = VerifiedCacheCapture(root / "checks", caches=(), bundle_supplier=supplier,
                                      session_factory=Session, fallback=fallback)
        except TypeError as exc:
            self.fail(f"Dynamic validated bundle supply is unsupported: {exc}")
        source = f"https://www.bilibili.com/video/{proof['bvid']}?p={proof['page']}"
        with patch("cached_capture.build_bundle", side_effect=build):
            result = fn(source, root / "out", allow_local_audio_read=True)
        return result, calls

    @staticmethod
    def _new_source_bundle():
        bundle = read_bundle(CACHES[0])
        # A source outside both legacy hard-coded entries.
        bundle["source_envelope"]["source_evidence"].update(bvid="BV1gD421u75J", page=2)
        return bundle

    def test_dynamic_nonlegacy_source_reuses_verified_saved_asr(self):
        bundle = self._new_source_bundle()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _, calls = self._supplied_capture(root, [bundle], bundle,
                fallback=lambda *a, **k: self.fail("matching supplied source ran ASR"))
            saved = read_bundle(root / "out")
            self.assertEqual(saved["source_envelope"]["transcription"],
                             bundle["source_envelope"]["transcription"])
            self.assertEqual(saved["source_envelope"]["source_evidence"]["bvid"], "BV1gD421u75J")
            self.assertEqual(calls, ["supplier", "resolve", "download"])

    def test_later_supplied_candidate_can_match_current_audio(self):
        bundle = self._new_source_bundle()
        stale = copy.deepcopy(bundle)
        stale["source_envelope"]["transcription"]["audio_sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._supplied_capture(root, [stale, bundle], bundle,
                fallback=lambda *a, **k: self.fail("first nonmatching candidate hid a later match"))
            proof = json.loads((root / "out/current-audio-reuse-proof.json").read_bytes())
            from bridge_contract import digest
            self.assertEqual(proof["saved_bundle_sha256"], digest(bundle))

    def test_wrong_current_audio_hash_falls_back_without_publishing_reuse(self):
        bundle = self._new_source_bundle()
        stale = copy.deepcopy(bundle)
        stale["source_envelope"]["transcription"]["audio_sha256"] = "0" * 64
        calls = []
        def fallback(*args, **kwargs):
            calls.append((args, kwargs))
            return "ordinary_capture"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result, stages = self._supplied_capture(root, [stale], bundle, fallback=fallback)
            self.assertEqual(result, "ordinary_capture")
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][1]["allow_local_audio_read"])
            self.assertEqual(stages, ["supplier", "resolve", "download"])
            self.assertFalse((root / "out").exists())
            self.assertEqual(json.loads((root / "checks/out/attempt.json").read_bytes())["state"],
                             "different_audio")

    def test_resolve_download_cid_mismatch_rejects_before_reuse_or_fallback(self):
        from video_action_agent.content_intake import IntakeError

        bundle = self._new_source_bundle()
        proof = copy.deepcopy(bundle["source_envelope"]["source_evidence"])
        audio_sha = bundle["source_envelope"]["transcription"]["audio_sha256"]
        calls = []
        class Session:
            def __init__(self, *args, **kwargs):
                pass
            def resolve(self):
                calls.append("resolve")
                return {**proof, "cid": proof["cid"] + 1}
            def download(self, path):
                calls.append("download")
                # The downloaded bytes match a cache, but contradict this resolve.
                return {**proof, "audio_sha256": audio_sha, "local_path": str(path)}
            def close(self):
                calls.append("close")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fn = VerifiedCacheCapture(root / "checks", caches=(),
                bundle_supplier=lambda ref: [bundle], session_factory=Session,
                fallback=lambda *a, **k: self.fail("identity mismatch must not fall back"))
            source = f"https://www.bilibili.com/video/{proof['bvid']}?p={proof['page']}"
            with patch("cached_capture.build_bundle",
                       side_effect=lambda *a, **k: self.fail("identity mismatch reached reuse")), \
                 patch("cached_capture.save_bundle",
                       side_effect=lambda *a, **k: self.fail("identity mismatch reached save")):
                with self.assertRaises(IntakeError) as rejected:
                    fn(source, root / "out", allow_local_audio_read=True)
            self.assertEqual(rejected.exception.code, "source_mismatch")
            self.assertEqual(calls, ["resolve", "download", "close"])
            self.assertFalse((root / "out").exists())

    def test_matching_bytes_keep_old_transcription_with_fresh_source_proof(self):
        bundle = read_bundle(CACHES[0])
        proof = copy.deepcopy(bundle["source_envelope"]["source_evidence"])
        proof["observed_at"] = "2026-10-15T00:00:00+08:00"
        audio_sha = bundle["source_envelope"]["transcription"]["audio_sha256"]
        calls = []
        class Session:
            def __init__(self, *args, **kwargs):
                pass
            def resolve(self):
                calls.append("resolve")
                return proof
            def download(self, path):
                calls.append("download")
                return {**proof, "audio_sha256": audio_sha, "local_path": str(path)}
            def close(self):
                pass
        def build(envelope, ref, acquisition, audio_path):
            value = copy.deepcopy(bundle)
            value.update(source_envelope=envelope, acquisition=acquisition)
            return value
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fn = VerifiedCacheCapture(root / "checks", session_factory=Session,
                                     fallback=lambda *a, **k: self.fail("unneeded ASR"))
            with patch("cached_capture.build_bundle", side_effect=build, create=True):
                fn("https://www.bilibili.com/video/BV1AJen6SEVQ?p=42", root / "out", allow_local_audio_read=True)
            saved = read_bundle(root / "out")
            self.assertEqual(saved["source_envelope"]["source_evidence"], proof)
            self.assertEqual(saved["source_envelope"]["transcription"], bundle["source_envelope"]["transcription"])
            self.assertEqual(calls, ["resolve", "download"])
            self.assertEqual(saved["acquisition"], "live_anonymous_audio")
            self.assertEqual(json.loads((root / "out/current-audio-reuse-proof.json").read_bytes())["new_asr_calls"], 0)
