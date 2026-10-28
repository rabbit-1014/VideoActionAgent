"""Bounded fixed-byte transport used only by saved-frame region analysis.

No file paths are reopened, no proxy or retry is enabled, and an HTTP body is
retained before its JSON/schema is inspected.  This adapter does not change the
historical vision_pipeline transport or its callers.
"""
import base64
import hashlib
import io
import json
import math
import os
import urllib.error
import urllib.request

from PIL import Image, UnidentifiedImageError

MODEL = "glm-5v-turbo"
ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
_BODY_LIMIT = 2_000_000
_USAGE_KEYS = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
               "cached_tokens", "reasoning_tokens", "audio_tokens", "accepted_prediction_tokens",
               "rejected_prediction_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"}
_USAGE_GROUPS = {"prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details"}


class RegionTransportError(ValueError):
    def __init__(self, code, *, network_dispatched=False, received=False):
        super().__init__(code)
        self.code = code
        self.network_dispatched = network_dispatched
        self.received = received


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _reject(code):
    raise RegionTransportError("saved_region_" + code)


def _usage(value):
    if not isinstance(value, dict):
        return None
    def finite_count(number):
        try:
            return type(number) in (int, float) and math.isfinite(number) and number >= 0
        except OverflowError:
            return False
    result = {}
    for key in _USAGE_KEYS:
        number = value.get(key)
        if finite_count(number):
            result[key] = number
    for key in _USAGE_GROUPS:
        if isinstance(value.get(key), dict):
            result[key] = {name: number for name, number in value[key].items() if name in _USAGE_KEYS
                           and finite_count(number)}
    return result


def _content(prompt, frames):
    if not isinstance(prompt, str) or not prompt.strip() or not isinstance(frames, list) or not 1 <= len(frames) <= 3:
        _reject("transport_input_invalid")
    content, seen = [{"type": "text", "text": prompt}], set()
    for frame in frames:
        if not isinstance(frame, dict) or not isinstance(frame.get("id"), str) or not frame["id"] or frame["id"] in seen:
            _reject("transport_frame_invalid")
        seen.add(frame["id"])
        raw = frame.get("image_bytes")
        if not isinstance(raw, bytes) or not raw or hashlib.sha256(raw).hexdigest() != frame.get("sha256"):
            _reject("transport_frame_hash_mismatch")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                image_format = image.format
                if image_format not in {"JPEG", "PNG"} or frame.get("image_format") != image_format:
                    _reject("transport_image_format_invalid")
                if image.size != (frame.get("width"), frame.get("height")):
                    _reject("transport_image_dimensions_mismatch")
                image.load()
        except (UnidentifiedImageError, OSError, SyntaxError):
            _reject("transport_image_invalid")
        mime = "image/jpeg" if image_format == "JPEG" else "image/png"
        content.extend([{"type": "text", "text": frame["id"]},
                        {"type": "image_url", "image_url": {"url": "data:" + mime + ";base64," +
                         base64.b64encode(raw).decode("ascii")}}])
    return content


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _receipt(body, key, *, status=None, rejected=False):
    # Exact secret-byte removal precedes both raw persistence and JSON parsing.
    redacted = body[:_BODY_LIMIT].replace(key.encode("utf-8"), b"[REDACTED]")
    truncated = len(body) > _BODY_LIMIT or len(redacted) > _BODY_LIMIT
    safe = redacted[:_BODY_LIMIT]
    result = {"received": True, "network_dispatched": True,
              "transport_state": "rejected" if rejected else "received_invalid",
              "raw_body_base64": base64.b64encode(safe).decode("ascii"),
              "raw_body_sha256": hashlib.sha256(safe).hexdigest(),
              "raw_body_truncated": truncated, "raw_body_redacted": redacted != body[:_BODY_LIMIT],
              "text": None, "model": None, "usage": None, "finish_reason": None}
    if status is not None:
        result["http_status"] = status
    if truncated:
        result["error_code"] = "reply_over_limit"
        return result
    try:
        value = json.loads(safe, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid number")))
        if not isinstance(value, dict):
            raise ValueError("invalid response object")
        model = value.get("model")
        result["model"] = model[:200] if isinstance(model, str) else None
        result["usage"] = _usage(value.get("usage"))
        if rejected:
            return result
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("missing choices")
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ValueError("missing content")
        reason = choices[0].get("finish_reason")
        result.update(text=message["content"], transport_state="received",
                      finish_reason=reason[:80] if isinstance(reason, str) else None)
    except (ValueError, UnicodeDecodeError, RecursionError):
        result["error_code"] = "invalid_provider_reply"
    return result


def transport(prompt, frames):
    """Return a retained receipt or an explicit zero/unknown-dispatch error."""
    key = os.environ.get("ZHIPUAI_API_KEY", "").strip()
    if not key:
        _reject("key_unavailable")
    content = _content(prompt, frames)
    payload = {"model": MODEL, "messages": [{"role": "user", "content": content}],
               "thinking": {"type": "enabled"}, "max_tokens": 4500, "stream": False}
    try:
        request = urllib.request.Request(ENDPOINT, data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    except Exception:
        _reject("request_preparation_failed")
    response_received, body = False, None
    try:
        response = opener.open(request, timeout=100)
        response_received = True
        with response:
            body = response.read(_BODY_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(_BODY_LIMIT + 1)
            return _receipt(body, key, status=exc.code, rejected=True)
        except Exception:
            return {"received": True, "network_dispatched": True, "transport_state": "rejected",
                    "http_status": exc.code, "raw_body_unavailable": True,
                    "text": None, "model": None, "usage": None, "finish_reason": None}
    except Exception:
        if body is not None:
            return _receipt(body, key)
        if response_received:
            return {"received": True, "network_dispatched": True, "transport_state": "received_invalid",
                    "raw_body_unavailable": True, "error_code": "reply_body_unavailable",
                    "text": None, "model": None, "usage": None, "finish_reason": None}
        raise RegionTransportError("saved_region_transport_outcome_unknown", network_dispatched=True) from None
    return _receipt(body, key)
