"""One fixed, direct DeepSeek JSON request; no retries or body persistence."""
import copy
import json
import math
import os
import time
import urllib.error
import urllib.request

MODEL = "deepseek-v4-pro"
ENDPOINT = "https://api.deepseek.com/chat/completions"
TIMEOUT_SECONDS = 300
MAX_TOKENS = 32768
_BODY_LIMIT = 2_000_000


class DeepSeekNativeProviderError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON key")
        result[name] = value
    return result


def _invalid_constant(_value):
    raise ValueError("invalid JSON number")


def _loads(value):
    return json.loads(value, object_pairs_hook=_unique, parse_constant=_invalid_constant)


def _safe_usage(value):
    """Retain provider counters without summing cache aliases or estimating."""
    if not isinstance(value, dict):
        return None

    def counter(number):
        return (type(number) is int and number >= 0
                or type(number) is float and math.isfinite(number) and number >= 0)

    safe = {key: value[key] for key in (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
        if key in value and counter(value[key])}
    for group, fields in (("prompt_tokens_details", ("cached_tokens",)),
                          ("completion_tokens_details", ("reasoning_tokens",))):
        nested = value.get(group)
        if isinstance(nested, dict):
            counters = {key: nested[key] for key in fields if key in nested and counter(nested[key])}
            if counters:
                safe[group] = counters
    return safe or None


class DeepSeekNativeProvider:
    """Sequential ``complete(system, user) -> str`` over one official endpoint."""

    model_id = MODEL
    timeout_seconds = TIMEOUT_SECONDS

    def __init__(self, *, reasoning_effort="high"):
        if type(reasoning_effort) is not str or reasoning_effort not in ("low", "high", "max"):
            raise DeepSeekNativeProviderError("invalid_reasoning_effort")
        self._reasoning_effort = reasoning_effort
        self._last_call_metrics = None

    @property
    def reasoning_effort(self):
        return self._reasoning_effort

    @property
    def last_call_metrics(self):
        return copy.deepcopy(self._last_call_metrics)

    def complete(self, system, user):
        started = time.monotonic()
        self._last_call_metrics = {
            "status": "running", "error_code": None, "elapsed_seconds": None,
            "request_bytes": None, "model": MODEL, "reasoning_effort": self.reasoning_effort,
            "stream": False, "usage": None, "received": False,
            "network_dispatched": False, "http_status": None, "calls": 0,
        }
        try:
            result = self._complete(system, user)
        except DeepSeekNativeProviderError as exc:
            self._last_call_metrics.update(status="failed", error_code=exc.code)
            raise
        except Exception:  # noqa: BLE001 -- never attach an arbitrary exception containing credentials
            self._last_call_metrics.update(status="failed", error_code="unexpected_error")
            raise DeepSeekNativeProviderError("unexpected_error") from None
        else:
            self._last_call_metrics["status"] = "succeeded"
            return result
        finally:
            self._last_call_metrics["elapsed_seconds"] = time.monotonic() - started

    def _read_payload(self, raw):
        if not isinstance(raw, bytes) or len(raw) > _BODY_LIMIT:
            raise DeepSeekNativeProviderError("reply_over_limit")
        try:
            payload = _loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise DeepSeekNativeProviderError("received_invalid") from None
        if not isinstance(payload, dict):
            raise DeepSeekNativeProviderError("received_invalid")
        self._last_call_metrics["usage"] = _safe_usage(payload.get("usage"))
        return payload

    def _complete(self, system, user):
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise DeepSeekNativeProviderError("api_key_missing")
        if (not isinstance(system, str) or not system.strip()
                or not isinstance(user, str) or not user.strip()):
            raise DeepSeekNativeProviderError("invalid_input")
        if key in system or key in user:
            raise DeepSeekNativeProviderError("secret_in_input")
        try:
            payload = {"model": MODEL, "messages": [
                {"role": "system", "content": system}, {"role": "user", "content": user}],
                "thinking": {"type": "enabled"}, "reasoning_effort": self.reasoning_effort,
                "max_tokens": MAX_TOKENS, "response_format": {"type": "json_object"}, "stream": False}
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._last_call_metrics["request_bytes"] = len(body)
            request = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
                "Authorization": "Bearer " + key, "Content-Type": "application/json"})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        except Exception:  # noqa: BLE001 -- preparation errors must not expose the Authorization header
            raise DeepSeekNativeProviderError("request_preparation_failed") from None

        self._last_call_metrics.update(network_dispatched=True, calls=1)
        try:
            response = opener.open(request, timeout=TIMEOUT_SECONDS)
            self._last_call_metrics["received"] = True
            with response:
                self._last_call_metrics["http_status"] = response.getcode()
                raw = response.read(_BODY_LIMIT + 1)
        except urllib.error.HTTPError as exc:
            self._last_call_metrics.update(received=True, http_status=exc.code)
            try:
                self._read_payload(exc.read(_BODY_LIMIT + 1))
            except Exception:  # noqa: BLE001 -- preserve HTTP rejection even when its optional body is invalid
                self._last_call_metrics["usage"] = None
            finally:
                exc.close()
            raise DeepSeekNativeProviderError("http_rejected") from None
        except Exception:  # noqa: BLE001 -- transport failures are classified without remote error text
            code = "received_invalid" if self._last_call_metrics["received"] else "request_outcome_unknown"
            raise DeepSeekNativeProviderError(code) from None
        if (type(self._last_call_metrics["http_status"]) is not int
                or not 200 <= self._last_call_metrics["http_status"] < 300):
            try:
                self._read_payload(raw)
            except DeepSeekNativeProviderError:
                self._last_call_metrics["usage"] = None
            raise DeepSeekNativeProviderError("http_rejected")

        payload = self._read_payload(raw)
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise DeepSeekNativeProviderError("received_invalid")
        choice = choices[0]
        if choice.get("finish_reason") != "stop":
            raise DeepSeekNativeProviderError("completion_not_finished")
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekNativeProviderError("received_invalid")
        try:
            parsed = _loads(content)
        except (ValueError, RecursionError):
            raise DeepSeekNativeProviderError("received_invalid") from None
        if not isinstance(parsed, dict):
            raise DeepSeekNativeProviderError("received_invalid")
        # Check decoded strings too, including escaped secret text in JSON values.
        if key in content or key in json.dumps(parsed, ensure_ascii=False):
            raise DeepSeekNativeProviderError("secret_in_reply")
        return content  # Never return, persist, or attach reasoning_content.
