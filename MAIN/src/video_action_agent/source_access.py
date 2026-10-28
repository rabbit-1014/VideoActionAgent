"""Local desktop source adapter. No cookies, arbitrary URLs, or public-server support."""

import json
import math
import re
import subprocess
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ADAPTER_VERSION = "opencli-bilibili-local-v1"
OPENCLI_VERSION = "1.8.6"
MAX_REPLY_BYTES = 2 * 1024 * 1024
_SECONDS_SUFFIX = re.compile(r"[0-9]+(?:\.[0-9]+)?s")
_CLI_ERROR_ENVELOPE = re.compile(rb"\Aok: false\r?\nerror:\r?\n  code: ([A-Z][A-Z0-9_]*)\r?\n")
# Explicit failure of this read path, never proof that the video lacks content.
SOURCE_READ_FAILURE_CODES = frozenset({
    "navigation_rejected", "adapter_unavailable", "invalid_reply", "source_unavailable",
    "subtitle_not_returned", "browser_connection_failed", "source_auth_required",
    "source_mismatch", "reply_too_large",
})


class SourceAccessError(ValueError):
    pass


@dataclass(frozen=True)
class VideoRef:
    bvid: str
    page: int = 1

    @classmethod
    def parse(cls, source):
        if not isinstance(source, str):
            raise SourceAccessError("invalid_source")
        source = source.strip()
        if re.fullmatch(r"BV[0-9A-Za-z]{10}", source):
            return cls(source)
        try:
            url = urlsplit(source)
            if (
                url.scheme != "https"
                or url.hostname not in {"www.bilibili.com", "bilibili.com"}
                or url.username
                or url.password
                or url.port not in {None, 443}
            ):
                raise SourceAccessError("unsupported_source")
            match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10})/?", url.path)
            pages = parse_qs(url.query, keep_blank_values=True).get("p", ["1"])
            if not match or len(pages) != 1 or not re.fullmatch(r"[1-9][0-9]{0,4}", pages[0]):
                raise SourceAccessError("invalid_source")
            return cls(match[1], int(pages[0]))
        except ValueError as exc:
            raise SourceAccessError("invalid_source") from exc


def _seconds(value):
    """Accept the numeric seconds OpenCLI may return as ``"<seconds>s"`` strings.

    The CLI has been observed returning ``from``/``to`` as strings such as ``"2.06s"``.
    Only a finite, non-negative decimal with a literal ``s`` suffix is coerced; anything
    else (signs, exponents, whitespace, booleans, non-finite floats) stays an error.
    """
    if isinstance(value, bool):
        raise SourceAccessError("invalid_reply")
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError as exc:
            raise SourceAccessError("invalid_reply") from exc
    elif isinstance(value, str) and _SECONDS_SUFFIX.fullmatch(value):
        number = float(value[:-1])
    else:
        raise SourceAccessError("invalid_reply")
    if not math.isfinite(number):
        raise SourceAccessError("invalid_reply")
    return number


def parse_reply(action: str, ref: VideoRef, raw: bytes) -> dict:
    if len(raw) > MAX_REPLY_BYTES:
        raise SourceAccessError("reply_too_large")
    try:
        rows = json.loads(raw)
        if not isinstance(rows, list):
            raise SourceAccessError("source_unavailable")
        if not rows:
            # Describes this read, not the absence of speech or subtitles in the video.
            raise SourceAccessError(
                "subtitle_not_returned" if action == "subtitle" else "source_unavailable"
            )
        if action == "video":
            fields = {r["field"]: r["value"] for r in rows}
            if len(fields) != len(rows) or fields.get("bvid") != ref.bvid:
                raise SourceAccessError("source_mismatch")
            if not isinstance(fields.get("title"), str) or not fields["title"].strip():
                raise SourceAccessError("invalid_reply")
            return {
                "kind": "metadata",
                "content_available": False,
                "title": fields["title"],
                "duration_display": fields.get("duration"),
                "parts_display": fields.get("parts"),
            }
        if action != "subtitle":
            raise SourceAccessError("unsupported_action")
        segments = []
        previous = -1
        for row in rows:
            start, end, content = _seconds(row["from"]), _seconds(row["to"]), row["content"]
            if (
                start < 0
                or start >= end
                or start < previous
                or not isinstance(content, str)
                or not content.strip()
            ):
                raise SourceAccessError("invalid_reply")
            segments.append({"start": start, "end": end, "text": content})
            previous = start
        return {
            "kind": "subtitle",
            "segments": segments,
            "automatically_verified_truth": False,
            "language": "provider_default_unknown",
            "coverage": "returned_segments_only",
        }
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        if isinstance(exc, SourceAccessError):
            raise
        raise SourceAccessError("invalid_reply") from exc


def _cli_failure_code(action, stdout, stderr):
    """Recognize pinned CLI error envelopes, never persist messages or trace paths."""
    for raw in (stderr, stdout):
        prefix = raw[:16 * 1024]
        code = None
        match = _CLI_ERROR_ENVELOPE.match(prefix)
        if match:
            code = match[1].decode("ascii")
        else:
            try:
                envelope = json.loads(prefix)
            except (ValueError, UnicodeError):
                continue
            if isinstance(envelope, dict) and envelope.get("ok") is False:
                error = envelope.get("error")
                if isinstance(error, dict) and isinstance(error.get("code"), str):
                    code = error["code"]
        if code == "EMPTY_RESULT":
            return "subtitle_not_returned" if action == "subtitle" else "source_unavailable"
        if code == "BROWSER_CONNECT":
            return "browser_connection_failed"
        if code == "AUTH_REQUIRED":
            return "source_auth_required"
    return "adapter_failed"


class LocalOpenCli:
    """An explicitly enabled operator-owned browser, not a shared-user credential service."""

    def __init__(self, node: str, entry: Path, *, navigation_workaround: bool = False):
        package = entry.parents[2] / "package.json"
        if json.loads(package.read_text(encoding="utf-8"))["version"] != OPENCLI_VERSION:
            raise SourceAccessError("adapter_version_changed")
        self.node, self.entry = node, entry
        self.navigation_workaround = navigation_workaround

    def fetch(self, action: str, ref: VideoRef) -> bytes:
        if action not in {"video", "subtitle"}:
            raise SourceAccessError("unsupported_action")
        # Validated scalar arguments, no shell interpolation or browser cookie export.
        args = [
            self.node,
            str(self.entry),
            "bilibili",
            action,
            ref.bvid,
            "--page",
            str(ref.page),
            "-f",
            "json",
            "--trace",
            "retain-on-failure" if self.navigation_workaround else "off",
            "--keep-tab",
            "false",
        ]
        try:
            result = subprocess.run(args, capture_output=True, timeout=90, check=False)
        except subprocess.TimeoutExpired as exc:
            raise SourceAccessError("adapter_timeout") from exc
        except OSError as exc:
            raise SourceAccessError("adapter_unavailable") from exc
        if result.returncode:
            # CLI stderr can carry browser/session details; never persist it as product data.
            if b"Navigation rejected" in result.stderr + result.stdout:
                raise SourceAccessError("navigation_rejected")
            raise SourceAccessError(_cli_failure_code(action, result.stdout, result.stderr))
        if len(result.stdout) > MAX_REPLY_BYTES:
            raise SourceAccessError("reply_too_large")
        return result.stdout


def payload_hash(raw):
    return sha256(raw).hexdigest()
