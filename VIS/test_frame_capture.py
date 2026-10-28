import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import frame_capture

MATERIAL = {
    "source_evidence": {
        "bvid": "BV1AJen6SEVQ",
        "page": 42,
        "cid": 4242,
        "duration_seconds": 1980.0,
    }
}


class FrameCaptureTests(unittest.TestCase):
    def test_exact_source_envelope_and_time_budget_are_validated(self):
        identity, duration = frame_capture._source(MATERIAL)
        self.assertEqual(identity, {"bvid": "BV1AJen6SEVQ", "page": 42, "cid": 4242})
        self.assertEqual(duration, 1980.0)
        self.assertEqual(frame_capture._requested_times([10, 20.5, 30], duration), [10.0, 20.5, 30.0])
        for times in ([10, 20, 30, 40], [1980], [-1], [float("nan")]):
            with self.subTest(times=times), self.assertRaises(frame_capture.FrameCaptureError):
                frame_capture._requested_times(list(times), duration)

    def test_success_is_bound_to_identity_and_only_valid_cache_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "frames"
            frame_bytes = b"offline-frame-proof"
            frame_sha = hashlib.sha256(frame_bytes).hexdigest()
            worker_result = {
                "schema_version": 1,
                "state": "available",
                "source": {"bvid": "BV1AJen6SEVQ", "page": 42, "cid": 4242},
                "observed_at": "2026-10-15T00:00:00+00:00",
                "frames": [{
                    "id": "f000", "requested_seconds": 12.0,
                    "actual_frame_pts_seconds": 12.04, "path": "frames/f000.jpg",
                    "sha256": frame_sha, "width": 2, "height": 3, "bytes": len(frame_bytes),
                }],
                "calls": {"info": 1, "playurl": 1, "ffmpeg_attempts": 1},
                "proof": {
                    "source": {"bvid": "BV1AJen6SEVQ", "page": 42, "cid": 4242},
                    "duration_seconds": 1980.0,
                    "observed_at": "2026-10-15T00:00:00+00:00",
                },
                "automatic_retry": False,
            }
            def fake_worker(payload, checkpoint):
                (Path(payload["output_dir"]) / "frames").mkdir()
                (Path(payload["output_dir"]) / "frames" / "f000.jpg").write_bytes(frame_bytes)
                return worker_result
            with patch.object(frame_capture, "_launch_worker", side_effect=fake_worker) as launch:
                result = frame_capture.capture_frames(MATERIAL, [12], output, checkpoint=lambda: None)
                cached = frame_capture.capture_frames(MATERIAL, [12], output, checkpoint=lambda: None)
            self.assertEqual(result, cached)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(result["frames"][0]["id"], "f000")
            other_material = {"source_evidence": {**MATERIAL["source_evidence"], "cid": 9999}}
            with self.assertRaises(frame_capture.FrameCaptureError):
                frame_capture.capture_frames(other_material, [12], output, checkpoint=lambda: None)
            self.assertEqual(launch.call_count, 1)

    def test_unknown_attempt_is_persisted_and_never_replayed(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "frames"
            def cancel_after_intent():
                if (output / "source-intent.json").exists():
                    raise RuntimeError("cancelled")
            with self.assertRaises(frame_capture.FrameCaptureError):
                frame_capture.capture_frames(MATERIAL, [12], output, checkpoint=cancel_after_intent)
            intent = json.loads((output / "source-intent.json").read_text(encoding="utf-8"))
            self.assertEqual(intent["source"]["cid"], 4242)
            with (
                patch.object(frame_capture, "_launch_worker") as launch,
                self.assertRaises(frame_capture.FrameCaptureError),
            ):
                frame_capture.capture_frames(MATERIAL, [12], output, checkpoint=lambda: None)
            launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
