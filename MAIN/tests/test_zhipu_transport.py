import json

import pytest

from video_action_agent.zhipu_provider import ZhipuProvider, ZhipuProviderError


class Reply:
    def __init__(self, text):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps({"choices": [{"message": {"content": self.text}}]}).encode()


def test_explicit_direct_transport_and_unchanged_default(monkeypatch):
    monkeypatch.setenv("ZHIPUAI_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Reply("configured"))

    class DirectOpener:
        def open(self, request, timeout):
            assert request.full_url == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
            return Reply("direct")

    def opener(handler):
        assert handler.proxies == {}
        return DirectOpener()

    monkeypatch.setattr("urllib.request.build_opener", opener)
    assert ZhipuProvider("glm-5.3", use_system_proxy=False).complete("system", "user") == "direct"
    assert ZhipuProvider("glm-5.3").complete("system", "user") == "configured"


@pytest.mark.parametrize("ending", ["stop", "length", "missing_done"])
def test_stream_only_returns_complete_content_with_explicit_effort(monkeypatch, ending):
    monkeypatch.setenv("ZHIPUAI_API_KEY", "test-only-not-a-real-key")

    class Stream(Reply):
        def __init__(self):
            events = [
                {"choices": [{"delta": {"reasoning_content": "private thinking"}}]},
                {"choices": [{"delta": {"content": "完整"}}]},
                {"choices": [{"delta": {"content": "正文"},
                               "finish_reason": "length" if ending == "length" else "stop"}]},
            ]
            self.lines = iter([*(b"data: " + json.dumps(event).encode() + b"\n" for event in events),
                               *([] if ending == "missing_done" else [b"data: [DONE]\n"])])

        def readline(self, limit=-1):
            return next(self.lines, b"")

    def send(request, timeout):
        payload = json.loads(request.data)
        assert payload["stream"] is True and payload["reasoning_effort"] == "low"
        return Stream()

    monkeypatch.setattr("urllib.request.urlopen", send)
    provider = ZhipuProvider("glm-5.3-flash", stream=True, reasoning_effort="low")
    if ending == "stop":
        assert provider.complete("system", "user") == "完整正文"
    else:
        with pytest.raises(ZhipuProviderError, match="unexpected_response"):
            provider.complete("system", "user")
