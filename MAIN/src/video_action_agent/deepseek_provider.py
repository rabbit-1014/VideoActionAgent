"""DeepSeek text provider adapter. The API key only comes from the local environment.

Nothing here writes logs, test snapshots, or diagnostics containing the key, the request
body, or the reply body. Failures raise ProviderError with a stable code only. The model
is fixed to ``deepseek-v4-pro``; other models are never called from this adapter.

This module has NOT made a real API call yet unless a delivery note says so explicitly.
"""

import json
import os
import urllib.request
from urllib.error import URLError

API_URL = "https://api.deepseek.com/chat/completions"
MODEL_ID = "deepseek-v4-pro"
API_KEY_ENV = "DEEPSEEK_API_KEY"
TIMEOUT_SECONDS = 60

__all__ = ["API_KEY_ENV", "MODEL_ID", "DeepSeekProvider", "ProviderError"]


class ProviderError(RuntimeError):
    """Provider failure. ``args[0]`` is a stable code; never includes key material."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class DeepSeekProvider:
    """Replaceable text provider with ``model_id`` and ``complete(system, user) -> str``."""

    model_id = MODEL_ID

    def complete(self, system: str, user: str) -> str:
        key = os.environ.get(API_KEY_ENV, "").strip()
        if not key:
            raise ProviderError("api_key_missing")
        body = json.dumps(
            {
                "model": MODEL_ID,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                # Structured extraction needs concise output; the API currently
                # defaults this model to high-effort thinking, which can leave a
                # local proxy waiting without response bytes for too long.
                "thinking": {"type": "disabled"},
                "max_tokens": 4096,
            },
            ensure_ascii=False,
        ).encode("utf-8")
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
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, OSError, ValueError) as exc:
            # The cause may carry network details; only the stable code leaves this module.
            raise ProviderError("request_failed") from exc
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("unexpected_response") from exc
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("unexpected_response")
        return content
