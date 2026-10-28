"""Decisive offline transport checks; no real provider request is permitted."""
import importlib
import importlib.util
import io
import json
import os
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch


class _Response:
    def __init__(self, body, *, status=200, read_error=None, status_error=None):
        self.body, self.status, self.read_error = body, status, read_error
        self.status_error = status_error
        self.read_limits = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def getcode(self):
        if self.status_error:
            raise self.status_error
        return self.status

    def read(self, maximum):
        self.read_limits.append(maximum)
        if self.read_error:
            raise self.read_error
        return self.body[:maximum]


class _Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class DeepSeekNativeProviderTests(unittest.TestCase):
    KEY = "offline-secret-do-not-log"

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("deepseek_native_provider"),
                             "fixed official native provider has not been implemented")
        return importlib.import_module("deepseek_native_provider")

    def receipt(self, *, content='{"answer":"完成"}', finish="stop", usage=True):
        value = {"id": "offline", "object": "chat.completion", "created": 1,
                 "model": "deepseek-v4-pro", "system_fingerprint": "offline",
                 "choices": [{"index": 0, "finish_reason": finish, "logprobs": None,
                              "message": {"role": "assistant", "content": content,
                                          "reasoning_content": "internal " + self.KEY}}]}
        if usage:
            value["usage"] = {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
                              "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60,
                              "prompt_tokens_details": {"cached_tokens": 40, "secret": self.KEY},
                              "completion_tokens_details": {"reasoning_tokens": 20},
                              "unknown_counter": 900, "secret": self.KEY}
        return json.dumps(value, ensure_ascii=False).encode("utf-8")

    def test_success_uses_fixed_direct_request_and_preserves_only_reported_safe_usage(self):
        module = self.module()
        response = _Response(self.receipt())
        opener = _Opener(response)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.KEY}), \
                patch.object(module.urllib.request, "build_opener", return_value=opener) as build:
            provider = module.DeepSeekNativeProvider()
            self.assertIsNone(provider.last_call_metrics)
            content = provider.complete("返回JSON对象。", "只回答问题。")
        self.assertEqual(json.loads(content), {"answer": "完成"})
        self.assertNotIn(self.KEY, content)
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 300)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.KEY)
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["max_tokens"], 32768)
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertIs(payload["stream"], False)
        self.assertNotIn(self.KEY, request.data.decode())
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {}
                            for h in handlers))
        redirects = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
        self.assertEqual(len(redirects), 1)
        self.assertIsNone(redirects[0].redirect_request(request, None, 302, "moved", {},
                                                       "https://other.invalid/"))
        self.assertEqual(response.read_limits, [2_000_001])
        metrics = provider.last_call_metrics
        self.assertEqual(metrics["status"], "succeeded")
        self.assertTrue(metrics["received"])
        self.assertTrue(metrics["network_dispatched"])
        self.assertEqual(metrics["http_status"], 200)
        self.assertEqual(metrics["request_bytes"], len(request.data))
        self.assertEqual(metrics["usage"], {
            "prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130,
            "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens_details": {"reasoning_tokens": 20}})
        self.assertGreaterEqual(metrics["elapsed_seconds"], 0)
        self.assertNotIn(self.KEY, json.dumps(metrics))
        self.assertNotIn("reasoning_content", json.dumps(metrics))
        metrics["usage"]["prompt_tokens_details"]["cached_tokens"] = -1
        self.assertEqual(provider.last_call_metrics["usage"]["prompt_tokens_details"]["cached_tokens"], 40)

    def test_missing_key_makes_zero_opener_calls_and_does_not_invent_usage(self):
        module = self.module()
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(module.urllib.request, "build_opener") as build:
            provider = module.DeepSeekNativeProvider()
            with self.assertRaisesRegex(module.DeepSeekNativeProviderError, "api_key_missing"):
                provider.complete("JSON", "question")
        build.assert_not_called()
        metrics = provider.last_call_metrics
        self.assertFalse(metrics["received"])
        self.assertFalse(metrics["network_dispatched"])
        self.assertIsNone(metrics["usage"])
        self.assertIsNone(metrics["http_status"])

    def test_explicit_effort_is_the_actual_wire_value_and_reported_configuration(self):
        module = self.module()
        for effort in ('low', 'max'):
            opener = _Opener(_Response(self.receipt()))
            with self.subTest(effort=effort), \
                    patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.KEY}), \
                    patch.object(module.urllib.request, 'build_opener', return_value=opener):
                provider = module.DeepSeekNativeProvider(reasoning_effort=effort)
                provider.complete('JSON', 'question')
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(json.loads(opener.calls[0][0].data)['reasoning_effort'], effort)
                self.assertEqual(provider.reasoning_effort, effort)
                self.assertEqual(provider.last_call_metrics['reasoning_effort'], effort)

    def test_unknown_effort_is_rejected_before_any_network_preparation(self):
        module = self.module()
        with patch.object(module.urllib.request, 'build_opener') as build:
            for effort in (None, True, [], 'xhigh', 'ultra', 'other'):
                with self.subTest(effort=effort), \
                        self.assertRaisesRegex(module.DeepSeekNativeProviderError,
                                               'invalid_reasoning_effort'):
                    module.DeepSeekNativeProvider(reasoning_effort=effort)
        build.assert_not_called()

    def test_http_rejection_and_redirect_never_follow_or_leak_remote_exception(self):
        module = self.module()
        for status in (401, 302):
            with self.subTest(status=status):
                error = urllib.error.HTTPError("https://evil.invalid/" + self.KEY, status,
                    self.KEY, {"Location": "https://evil.invalid/"}, io.BytesIO(self.receipt()))
                opener = _Opener(error=error)
                with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.KEY}), \
                        patch.object(module.urllib.request, "build_opener", return_value=opener):
                    provider = module.DeepSeekNativeProvider()
                    with self.assertRaises(module.DeepSeekNativeProviderError) as caught:
                        provider.complete("JSON", "question")
                self.assertEqual(len(opener.calls), 1)
                self.assertNotIn(self.KEY, str(caught.exception))
                self.assertTrue(caught.exception.__suppress_context__)
                metrics = provider.last_call_metrics
                self.assertEqual(metrics["status"], "failed")
                self.assertEqual(metrics["error_code"], "http_rejected")
                self.assertEqual(metrics["http_status"], status)
                self.assertTrue(metrics["received"])
                self.assertTrue(metrics["network_dispatched"])
                self.assertEqual(metrics["usage"]["prompt_cache_hit_tokens"], 40)
                self.assertNotIn(self.KEY, json.dumps(metrics))

    def test_received_invalid_bodies_truncation_empty_and_secret_echo_are_rejected_once(self):
        module = self.module()
        bodies = (b"not-json " + self.KEY.encode(), self.receipt(finish="length"),
                  self.receipt(content=""), self.receipt(content="bad JSON"),
                  self.receipt(content=json.dumps({"echo": self.KEY})), b"x" * 2_000_001,
                  b'{"choices":[],"choices":[]}')
        for body in bodies:
            with self.subTest(kind=body[:20]):
                opener = _Opener(_Response(body))
                with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.KEY}), \
                        patch.object(module.urllib.request, "build_opener", return_value=opener):
                    provider = module.DeepSeekNativeProvider()
                    with self.assertRaises(module.DeepSeekNativeProviderError) as caught:
                        provider.complete("JSON", "question")
                self.assertEqual(len(opener.calls), 1)
                self.assertNotIn(self.KEY, str(caught.exception))
                self.assertTrue(provider.last_call_metrics["received"])
                self.assertTrue(provider.last_call_metrics["network_dispatched"])
                self.assertEqual(provider.last_call_metrics["status"], "failed")
                self.assertNotIn(self.KEY, json.dumps(provider.last_call_metrics))

    def test_unreceived_unknown_is_distinct_from_received_body_read_failure(self):
        module = self.module()
        cases = ((_Opener(error=urllib.error.URLError(self.KEY)), False, "request_outcome_unknown"),
                 (_Opener(_Response(b"", read_error=OSError(self.KEY))), True, "received_invalid"),
                 (_Opener(_Response(b"", status_error=OSError(self.KEY))), True, "received_invalid"))
        for opener, received, code in cases:
            with self.subTest(received=received), \
                    patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.KEY}), \
                    patch.object(module.urllib.request, "build_opener", return_value=opener):
                provider = module.DeepSeekNativeProvider()
                with self.assertRaises(module.DeepSeekNativeProviderError) as caught:
                    provider.complete("JSON", "question")
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(provider.last_call_metrics["received"], received)
                self.assertEqual(provider.last_call_metrics["error_code"], code)
                self.assertIsNone(provider.last_call_metrics["usage"])
                self.assertNotIn(self.KEY, str(caught.exception))

    def test_absent_or_non_numeric_usage_remains_unknown_without_cache_double_counting(self):
        module = self.module()
        for absent in (True, False):
            with self.subTest(absent=absent):
                payload = json.loads(self.receipt(usage=False))
                if not absent:
                    payload["usage"] = {"prompt_tokens": True, "total_tokens": -1,
                                        "completion_tokens": "30", "prompt_cache_hit_tokens": None}
                opener = _Opener(_Response(json.dumps(payload).encode()))
                with patch.dict(os.environ, {"DEEPSEEK_API_KEY": self.KEY}), \
                        patch.object(module.urllib.request, "build_opener", return_value=opener):
                    provider = module.DeepSeekNativeProvider()
                    provider.complete("JSON", "question")
                self.assertIsNone(provider.last_call_metrics["usage"])


if __name__ == "__main__":
    unittest.main()
