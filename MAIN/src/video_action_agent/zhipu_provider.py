"""Zhipu (智谱) text provider adapter. Replaceable: it never falls back to another
provider or to a generator model, and it never invents a model name.

The model id must be given by explicit local configuration (constructor argument); the
API key is read only from the local environment. Missing key or model fails with a stable
code before any network access.

Request/response shape verified against the official public docs on 2026-09-25:
- POST ``https://open.bigmodel.cn/api/paas/v4/chat/completions``
  (docs: /cn/guide/develop/http/introduction and the OpenAPI spec behind
  /api-reference/模型-api/对话补全 at docs.bigmodel.cn)
- headers ``Content-Type: application/json`` and ``Authorization: Bearer <API_KEY>``
- request ``{"model": <configured id>, "messages": [{"role": "system"}, {"role": "user"}],
  "temperature": <0.0-1.0>}``; ``messages`` must contain a user message
- response ``{"choices": [{"message": {"role": "assistant", "content": <text>}, ...}], ...}``
- errors ``{"error": {"code": <str>, "message": <str>}}``
- model ids are caller configuration (docs currently list glm-5.3, glm-5.2, glm-4.6 and
  others; this module pins none of them)

Nothing here writes logs, snapshots, or diagnostics containing the key, the request body,
or the reply body. Failures raise ZhipuProviderError with a stable code only. This module
has NOT made a real API call unless a delivery note says so explicitly.
"""

import copy
import json
import math
import os
import time
import urllib.request
from urllib.error import HTTPError, URLError

API_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
API_KEY_ENV = "ZHIPUAI_API_KEY"
TIMEOUT_SECONDS = 60

__all__ = ["API_KEY_ENV", "API_URL", "TIMEOUT_SECONDS", "ZhipuProvider", "ZhipuProviderError"]


class ZhipuProviderError(RuntimeError):
    """Provider failure. ``args[0]`` is a stable code; never includes key material."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ZhipuProvider:
    """Replaceable review provider with ``model_id`` and ``complete(system, user) -> str``."""

    def __init__(self, model_id: str, *, timeout_seconds: int = TIMEOUT_SECONDS,
                 use_system_proxy: bool = True, stream: bool = False,
                 reasoning_effort: str | None = None, json_object: bool = False):
        if not isinstance(model_id, str) or not model_id.strip():
            raise ZhipuProviderError("model_not_configured")
        self.model_id = model_id.strip()
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 300:
            raise ZhipuProviderError("invalid_timeout")
        self.timeout_seconds = timeout_seconds
        if type(use_system_proxy) is not bool:
            raise ZhipuProviderError("invalid_transport")
        self.use_system_proxy = use_system_proxy
        if type(stream) is not bool:
            raise ZhipuProviderError("invalid_transport")
        self.stream = stream
        if type(json_object) is not bool:
            raise ZhipuProviderError("invalid_transport")
        self.json_object = json_object
        if reasoning_effort is not None and (
            reasoning_effort not in ("low", "high", "max")
            or self.model_id not in ("glm-5.3", "glm-5.3-flash")
        ):
            raise ZhipuProviderError("invalid_reasoning_effort")
        self.reasoning_effort = reasoning_effort
        self._last_call_metrics = None

    @property
    def last_call_metrics(self) -> dict | None:
        """Defensive snapshot of the latest call; None before any call.

        Usage is provider-reported only: missing counters stay absent, and an
        entirely absent/unsupported usage is None. No token estimates or costs.
        This latest-call view is intended for sequential use of each instance.
        """
        return copy.deepcopy(self._last_call_metrics)

    def complete(self, system: str, user: str) -> str:
        started = time.monotonic()
        self._last_call_metrics = {
            "status": "running", "error_code": None, "elapsed_seconds": None,
            "request_bytes": None, "model": self.model_id,
            "reasoning_effort": self.reasoning_effort or (
                "high" if self.model_id == "glm-5.3" else None),
            "stream": self.stream, "usage": None,
        }
        try:
            content = self._complete(system, user)
        except BaseException as exc:
            self._last_call_metrics["status"] = "failed"
            self._last_call_metrics["error_code"] = (
                exc.code if isinstance(exc, ZhipuProviderError) else "unexpected_error")
            raise
        else:
            self._last_call_metrics["status"] = "succeeded"
            return content
        finally:
            self._last_call_metrics["elapsed_seconds"] = time.monotonic() - started

    def _record_usage(self, payload):
        if isinstance(payload, dict) and "usage" in payload:
            self._last_call_metrics["usage"] = _safe_usage(payload["usage"])

    def _complete(self, system: str, user: str) -> str:
        key = os.environ.get(API_KEY_ENV, "").strip()
        if not key:
            raise ZhipuProviderError("api_key_missing")
        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        if self.reasoning_effort is not None:
            payload["reasoning_effort"] = self.reasoning_effort
        elif self.model_id == "glm-5.3":
            # GLM-5.3 defaults to max thinking. High still reviews the full draft
            # while avoiding the local 60-second timeout seen with the default.
            payload["reasoning_effort"] = "high"
        if self.stream:
            payload["stream"] = True
        if self.json_object:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._last_call_metrics["request_bytes"] = len(body)
        request = urllib.request.Request(
            API_URL,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        try:
            deadline = time.monotonic() + self.timeout_seconds
            open_request = (urllib.request.urlopen if self.use_system_proxy else
                            urllib.request.build_opener(urllib.request.ProxyHandler({})).open)
            with open_request(request, timeout=min(self.timeout_seconds, 30)
                              if self.stream else self.timeout_seconds) as response:
                if self.stream:
                    return _read_stream(response, deadline, self._record_usage)
                raw_payload = response.read()
        except TimeoutError as exc:
            raise ZhipuProviderError("request_timeout") from exc
        except HTTPError as exc:
            raise ZhipuProviderError("http_error") from exc
        except (URLError, OSError) as exc:
            raise ZhipuProviderError("network_error") from exc
        try:
            payload = json.loads(raw_payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ZhipuProviderError("unexpected_response") from exc
        self._record_usage(payload)
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ZhipuProviderError("unexpected_response") from exc
        if not isinstance(content, str) or not content.strip():
            raise ZhipuProviderError("unexpected_response")
        return content


def _read_stream(response, deadline, record_usage=None):
    """Only complete stop+[DONE] content is usable; reasoning is never retained."""
    content = []
    finish_reason = None
    total_bytes = 0
    while time.monotonic() < deadline:
        line = response.readline(2_000_001)
        if time.monotonic() >= deadline:
            raise ZhipuProviderError("request_timeout")
        total_bytes += len(line)
        if total_bytes > 2_000_000:
            raise ZhipuProviderError("unexpected_response")
        if not line:
            break
        if not line.startswith(b"data:"):
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            if finish_reason == "stop" and content:
                return "".join(content)
            break
        try:
            event = json.loads(data)
            if record_usage is not None:
                record_usage(event)
            choices = event.get("choices", [])
            if choices:
                choice = choices[0]
                text = choice.get("delta", {}).get("content")
                if text is not None:
                    if not isinstance(text, str):
                        raise ValueError("invalid_content_type")
                    if text:
                        content.append(text)
                finish_reason = choice.get("finish_reason") or finish_reason
        except (ValueError, AttributeError, TypeError, IndexError) as exc:
            raise ZhipuProviderError("unexpected_response") from exc
    if time.monotonic() >= deadline:
        raise ZhipuProviderError("request_timeout")
    raise ZhipuProviderError("unexpected_response")


def _safe_usage(usage):
    """Retain only explicit, nonnegative finite numeric provider counters."""
    if not isinstance(usage, dict):
        return None
    fields = {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None,
        "prompt_tokens_details": ("cached_tokens", "cache_read_tokens", "cache_write_tokens"),
        "completion_tokens_details": ("reasoning_tokens",),
    }
    safe = {}
    for name, nested in fields.items():
        value = usage.get(name)
        if nested is not None:
            if isinstance(value, dict):
                counters = {key: value[key] for key in nested
                            if key in value and _is_counter(value[key])}
                if counters:
                    safe[name] = counters
        elif _is_counter(value):
            safe[name] = value
    return safe or None


def _is_counter(value):
    return (type(value) in (int, float) and value >= 0
            and (type(value) is int or math.isfinite(value)))
