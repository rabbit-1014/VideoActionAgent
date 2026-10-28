"""Request-local fake opener tests; never installs a global network guard."""
import base64
import hashlib
import importlib
import io
import json
import os
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from PIL import Image


class Response:
    def __init__(self, body):
        self.body = body
        self.limits = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, limit):
        self.limits.append(limit)
        return self.body[:limit]


class UnreadableResponse(Response):
    def read(self, _limit):
        raise OSError("offline body read failed after response headers")


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.requests = response, error, []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class SavedRegionTransportTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("saved_region_transport"),
                             "fixed-byte region transport has not been implemented")
        return importlib.import_module("saved_region_transport")

    def frame(self, image_format):
        buffer = io.BytesIO()
        Image.new("RGB", (12, 8), (30, 60, 90)).save(buffer, format=image_format)
        raw = buffer.getvalue()
        return {"id": "frame", "path": "must-not-be-read.jpg", "image_bytes": raw,
                "sha256": hashlib.sha256(raw).hexdigest(), "image_format": image_format,
                "width": 12, "height": 8, "actual_frame_pts_seconds": 1.0}

    def test_real_png_jpeg_payloads_use_fixed_bytes_and_correct_mime(self):
        module = self.module()
        for image_format, mime in (("PNG", "image/png"), ("JPEG", "image/jpeg")):
            with self.subTest(image_format=image_format):
                frame = self.frame(image_format)
                response = Response(json.dumps({"choices": [{"message": {"content": "{\"frames\":[]}"},
                    "finish_reason": "stop"}], "model": "glm-5v-turbo", "usage": {
                        "prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12,
                        "completion_tokens_details": {"reasoning_tokens": 2}, "unsafe_extra": "must-drop"}}).encode())
                opener = Opener(response)
                with patch.dict(os.environ, {"ZHIPUAI_API_KEY": "offline-secret"}), \
                        patch.object(module.urllib.request, "build_opener", return_value=opener), \
                        patch.object(Path, "read_bytes", side_effect=AssertionError("path must not be reopened")):
                    answer = module.transport("region-only prompt", [frame])
                self.assertEqual(len(opener.requests), 1)
                request, timeout = opener.requests[0]
                self.assertEqual(timeout, 100)
                payload = json.loads(request.data)
                image_url = next(c["image_url"]["url"] for c in payload["messages"][0]["content"] if c["type"] == "image_url")
                self.assertTrue(image_url.startswith(f"data:{mime};base64,"))
                self.assertEqual(base64.b64decode(image_url.split(",", 1)[1]), frame["image_bytes"])
                self.assertTrue(answer["network_dispatched"])
                self.assertTrue(answer["received"])
                self.assertEqual(answer["transport_state"], "received")
                self.assertEqual(answer["usage"]["total_tokens"], 12)
                self.assertNotIn("unsafe_extra", answer["usage"])
                self.assertEqual(base64.b64decode(answer["raw_body_base64"]), response.body)
                self.assertEqual(response.limits, [2_000_001])

    def test_received_invalid_or_rejected_body_is_retained_and_distinct_from_unknown(self):
        module = self.module()
        bodies = [b'not-json offline-secret', b'{"error":"offline-secret"}',
                  b'{"choices":[],"model":"glm-5v-turbo"}', b"x" * 2_000_001]
        for body in bodies:
            with self.subTest(body_length=len(body)):
                opener = Opener(Response(body))
                with patch.dict(os.environ, {"ZHIPUAI_API_KEY": "offline-secret"}), \
                        patch.object(module.urllib.request, "build_opener", return_value=opener):
                    answer = module.transport("regions", [self.frame("PNG")])
                self.assertEqual(answer["transport_state"], "received_invalid")
                self.assertTrue(answer["received"])
                self.assertTrue(answer["network_dispatched"])
                self.assertNotIn(b"offline-secret", base64.b64decode(answer["raw_body_base64"]))
                self.assertEqual(answer["raw_body_truncated"], len(body) > 2_000_000)
                self.assertLessEqual(len(base64.b64decode(answer["raw_body_base64"])), 2_000_000)
        rejection = urllib.error.HTTPError("https://example.invalid", 403, "denied", {}, io.BytesIO(b"denied offline-secret"))
        opener = Opener(error=rejection)
        with patch.dict(os.environ, {"ZHIPUAI_API_KEY": "offline-secret"}), \
                patch.object(module.urllib.request, "build_opener", return_value=opener):
            answer = module.transport("regions", [self.frame("JPEG")])
        self.assertEqual(answer["transport_state"], "rejected")
        self.assertEqual(answer["http_status"], 403)
        self.assertTrue(answer["received"])
        self.assertNotIn(b"offline-secret", base64.b64decode(answer["raw_body_base64"]))
        opener = Opener(error=TimeoutError("offline timeout"))
        with patch.dict(os.environ, {"ZHIPUAI_API_KEY": "offline-secret"}), \
                patch.object(module.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(module.RegionTransportError) as caught:
                module.transport("regions", [self.frame("JPEG")])
        self.assertTrue(caught.exception.network_dispatched)
        self.assertFalse(caught.exception.received)

    def test_missing_key_or_bad_pinned_hash_performs_zero_network_calls(self):
        module = self.module()
        for problem in ("missing_key", "bad_hash", "too_many_frames", "truncated_image"):
            with self.subTest(problem=problem):
                frame = self.frame("PNG")
                if problem == "bad_hash":
                    frame["sha256"] = "0" * 64
                elif problem == "truncated_image":
                    frame = self.frame("JPEG")
                    frame["image_bytes"] = frame["image_bytes"][:-5]
                    frame["sha256"] = hashlib.sha256(frame["image_bytes"]).hexdigest()
                frames = [frame] * 4 if problem == "too_many_frames" else [frame]
                opener = Opener(Response(b"{}"))
                with patch.dict(os.environ, {"ZHIPUAI_API_KEY": "" if problem == "missing_key" else "offline-secret"}), \
                        patch.object(module.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(module.RegionTransportError) as caught:
                        module.transport("regions", frames)
                self.assertFalse(caught.exception.network_dispatched)
                self.assertFalse(caught.exception.received)
                self.assertEqual(opener.requests, [])

    def test_received_headers_with_unreadable_body_are_not_an_unknown_dispatch(self):
        module = self.module()
        opener = Opener(UnreadableResponse(b""))
        with patch.dict(os.environ, {"ZHIPUAI_API_KEY": "offline-secret"}), \
                patch.object(module.urllib.request, "build_opener", return_value=opener):
            answer = module.transport("regions", [self.frame("PNG")])
        self.assertTrue(answer["network_dispatched"])
        self.assertTrue(answer["received"])
        self.assertTrue(answer["raw_body_unavailable"])
        self.assertEqual(answer["transport_state"], "received_invalid")

    def test_redaction_cannot_expand_the_retained_body_over_its_limit(self):
        module = self.module()
        answer = module._receipt(b"x" * 300_000, "x")
        self.assertTrue(answer["raw_body_truncated"])
        self.assertTrue(answer["raw_body_redacted"])
        self.assertLessEqual(len(base64.b64decode(answer["raw_body_base64"])), 2_000_000)

    def test_unrepresentable_usage_numbers_do_not_lose_a_received_body(self):
        module = self.module()
        body = json.dumps({"choices": [{"message": {"content": "{\"frames\":[]}"}, "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 4, "total_tokens": 10**400,
                                     "completion_tokens_details": {"reasoning_tokens": 10**400}}}).encode()
        opener = Opener(Response(body))
        with patch.dict(os.environ, {"ZHIPUAI_API_KEY": "offline-secret"}), \
                patch.object(module.urllib.request, "build_opener", return_value=opener):
            answer = module.transport("regions", [self.frame("PNG")])
        self.assertTrue(answer["received"])
        self.assertEqual(answer["transport_state"], "received")
        self.assertEqual(answer["usage"]["prompt_tokens"], 4)
        self.assertNotIn("total_tokens", answer["usage"])
        self.assertNotIn("reasoning_tokens", answer["usage"]["completion_tokens_details"])
        self.assertEqual(base64.b64decode(answer["raw_body_base64"]), body)


if __name__ == "__main__":
    unittest.main()
