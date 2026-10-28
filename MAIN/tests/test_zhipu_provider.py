import importlib.util
import json
from urllib.error import URLError

import pytest


def provider_mod():
    assert importlib.util.find_spec("video_action_agent.zhipu_provider"), "provider missing"
    return importlib.import_module("video_action_agent.zhipu_provider")


class StubResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize(
    "case,expected",
    [("key_missing", "api_key_missing"), ("model_missing", "model_not_configured")],
)
def test_missing_key_or_model_never_touches_the_network(case, expected, monkeypatch):
    mod = provider_mod()
    monkeypatch.delenv(mod.API_KEY_ENV, raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("no network call is allowed without an explicit real-call run")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    if case == "model_missing":
        with pytest.raises(mod.ZhipuProviderError) as excinfo:
            mod.ZhipuProvider("   ")
    else:
        with pytest.raises(mod.ZhipuProviderError) as excinfo:
            mod.ZhipuProvider("glm-local-config-test").complete("审查契约", "审查数据")
    assert excinfo.value.code == expected
    assert "local-test-key" not in str(excinfo.value)


def test_offline_request_and_response_follow_the_documented_api_shape(monkeypatch):
    mod = provider_mod()
    monkeypatch.setenv(mod.API_KEY_ENV, "local-test-key")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["request"] = request
        body = {"choices": [{"message": {"role": "assistant", "content": '{"ok": true}'}}]}
        return StubResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    provider = mod.ZhipuProvider("glm-local-config-test")
    assert provider.complete("审查契约", "审查数据") == '{"ok": true}'
    request = captured["request"]
    assert request.full_url == mod.API_URL
    assert request.get_header("Authorization") == "Bearer local-test-key"
    body = json.loads(request.data.decode("utf-8"))
    assert body["model"] == "glm-local-config-test"
    assert body["messages"] == [
        {"role": "system", "content": "审查契约"},
        {"role": "user", "content": "审查数据"},
    ]
    assert "reasoning_effort" not in body


def test_glm_53_review_uses_high_reasoning_effort(monkeypatch):
    mod = provider_mod()
    monkeypatch.setenv(mod.API_KEY_ENV, "local-test-key")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return StubResponse(b'{"choices": [{"message": {"content": "{}"}}]}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    mod.ZhipuProvider("glm-5.3").complete("审查契约", "审查数据")
    assert captured["body"]["reasoning_effort"] == "high"


def test_json_mode_is_explicit_and_does_not_repair_invalid_content(monkeypatch):
    mod = provider_mod()
    monkeypatch.setenv(mod.API_KEY_ENV, "local-test-key")
    bodies = []

    def fake_urlopen(request, timeout=None):
        bodies.append(json.loads(request.data.decode("utf-8")))
        body = {"choices": [{"message": {"content": '{"broken": ]}'}}]}
        return StubResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    assert mod.ZhipuProvider("glm-5.3", json_object=True).complete("JSON only", "data") == '{"broken": ]}'
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert bodies[0]["reasoning_effort"] == "high"
    mod.ZhipuProvider("glm-5.3").complete("Original text", "data")
    assert "response_format" not in bodies[1]
    with pytest.raises(mod.ZhipuProviderError) as excinfo:
        mod.ZhipuProvider("glm-5.3", json_object="yes")
    assert excinfo.value.code == "invalid_transport"


def test_malformed_response_raises_stable_code_and_leaks_nothing(monkeypatch):
    mod = provider_mod()
    monkeypatch.setenv(mod.API_KEY_ENV, "local-test-key")

    def fake_urlopen(request, timeout=None):
        return StubResponse(b'{"error": {"code": "1302", "message": "secret-server-detail"}}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(mod.ZhipuProviderError) as excinfo:
        mod.ZhipuProvider("glm-local-config-test").complete("审查契约", "审查数据")
    assert excinfo.value.code == "unexpected_response"
    assert "secret-server-detail" not in str(excinfo.value)
    assert "local-test-key" not in str(excinfo.value)


@pytest.mark.parametrize(
    "failure,expected",
    [(TimeoutError("slow"), "request_timeout"), (URLError("offline"), "network_error")],
)
def test_transport_failures_have_stable_types_and_are_never_retried(
    failure, expected, monkeypatch
):
    mod = provider_mod()
    monkeypatch.setenv(mod.API_KEY_ENV, "local-test-key")
    calls = 0

    def fail_once(request, timeout=None):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr("urllib.request.urlopen", fail_once)
    with pytest.raises(mod.ZhipuProviderError) as excinfo:
        mod.ZhipuProvider("glm-local-config-test").complete("审查契约", "审查数据")

    assert calls == 1
    assert excinfo.value.code == expected
    assert "slow" not in str(excinfo.value) and "offline" not in str(excinfo.value)


@pytest.mark.parametrize("stream", [False, True])
def test_call_metrics_keep_only_provider_numeric_usage(stream, monkeypatch):
    import io

    mod = provider_mod()
    monkeypatch.setenv(mod.API_KEY_ENV, "local-test-key")
    usage = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16,
             "prompt_tokens_details": {"cached_tokens": 3, "secret": "private"},
             "completion_tokens_details": {"reasoning_tokens": 2},
             "messages": "sensitive", "headers": "sensitive", "api_key": "local-test-key"}
    captured = {}

    def reply(request, timeout=None):
        captured["bytes"] = len(request.data)
        if stream:
            events = [
                {"choices": [{"delta": {"content": "正文"}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
            data = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
            return io.BytesIO((data + "data: [DONE]\n\n").encode())
        return StubResponse(json.dumps({"choices": [{"message": {"content": "正文"}}],
                                        "usage": usage}).encode())

    monkeypatch.setattr("urllib.request.urlopen", reply)
    provider = mod.ZhipuProvider("glm-5.3", stream=stream)
    assert provider.last_call_metrics is None
    assert provider.complete("private system", "private user") == "正文"
    metrics = provider.last_call_metrics
    assert metrics["status"] == "succeeded"
    assert metrics["elapsed_seconds"] >= 0
    assert metrics["request_bytes"] == captured["bytes"]
    assert (metrics["model"], metrics["reasoning_effort"], metrics["stream"]) == (
        "glm-5.3", "high", stream)
    assert metrics["usage"] == {k: v for k, v in usage.items()
                                if k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                         "completion_tokens_details")} | {
        "prompt_tokens_details": {"cached_tokens": 3}}
    assert "private" not in json.dumps(metrics)
    assert "local-test-key" not in json.dumps(metrics)
    metrics["usage"]["total_tokens"] = 999
    assert provider.last_call_metrics["usage"]["total_tokens"] == 16


@pytest.mark.parametrize("next_call", ["missing_usage", "missing_key", "usage_only_stream"])
def test_metrics_reset_previous_usage_on_every_call(next_call, monkeypatch):
    import io

    mod = provider_mod()
    monkeypatch.setenv(mod.API_KEY_ENV, "local-test-key")
    provider = mod.ZhipuProvider("glm-test")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: StubResponse(
        b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":7}}'))
    provider.complete("s", "u")
    assert provider.last_call_metrics["usage"] == {"total_tokens": 7}
    if next_call == "missing_key":
        monkeypatch.delenv(mod.API_KEY_ENV)
    elif next_call == "usage_only_stream":
        provider.stream = True
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: io.BytesIO(
            b'data: {"choices":[],"usage":{"total_tokens":2}}\n\ndata: [DONE]\n'))
    else:
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: StubResponse(
            b'{"choices":[{"message":{"content":"ok"}}]}'))
    if next_call == "missing_usage":
        assert provider.complete("s", "u") == "ok"
        assert provider.last_call_metrics["status"] == "succeeded"
    else:
        with pytest.raises(mod.ZhipuProviderError):
            provider.complete("s", "u")
        assert provider.last_call_metrics["status"] == "failed"
    metrics = provider.last_call_metrics
    assert metrics["elapsed_seconds"] >= 0
    assert metrics["usage"] == ({"total_tokens": 2} if next_call == "usage_only_stream" else None)
