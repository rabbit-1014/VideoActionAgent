"""Explicit anonymous, exact-part audio intake. No browser credentials or retries.

This standalone adapter does not authorize work or write product material. Callers
must independently enforce task owner/version/cancellation and audio action scope.
The optional bilibili-api-python dependency lives in a separate process environment.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse


class AudioSourceError(Exception):
    """Stable safe failure: upstream raw errors can contain signed stream URLs."""


class AudioClient(Protocol):
    async def get_info(self) -> dict: ...

    async def get_download_url(self, *, page_index: int, cid: int) -> dict: ...


@dataclass(frozen=True)
class ResolvedAudio:
    bvid: str
    page: int
    cid: int
    part_title: str
    collection_title: str
    duration_seconds: float
    stream_duration_seconds: float
    observed_at: str
    stream_url: str = field(repr=False, compare=False)

    def proof(self) -> dict:
        return {
            "bvid": self.bvid, "page": self.page, "cid": self.cid,
            "part_title": self.part_title, "collection_title": self.collection_title,
            "duration_seconds": self.duration_seconds,
            "stream_duration_seconds": self.stream_duration_seconds,
            "observed_at": self.observed_at, "access_mode": "anonymous_public_client",
            "identity_evidence": "view.pages + exact page_index/cid request + CDN path CID",
            "browser_session_ref": None,
        }


def _stream_identity(url: str, cid: int) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return (
        parsed.scheme == "https" and host.endswith(".bilivideo.com")
        and str(cid) in parsed.path.split("/")
    )


async def resolve_audio(bvid: str, page: int, client: AudioClient) -> ResolvedAudio:
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) or type(page) is not int or page < 1:
        raise AudioSourceError("invalid_reference")
    try:
        info = await client.get_info()
    except Exception:  # noqa: BLE001 -- foreign client errors may contain signed URLs
        raise AudioSourceError("platform_request_failed") from None
    if not isinstance(info, dict) or info.get("bvid") != bvid:
        raise AudioSourceError("identity_mismatch")
    pages = info.get("pages")
    if not isinstance(pages, list) or any(not isinstance(p, dict) for p in pages):
        raise AudioSourceError("identity_unknown")
    matches = [p for p in pages if type(p.get("page")) is int and p["page"] == page]
    if len(matches) != 1:
        raise AudioSourceError("identity_unknown")
    selected = matches[0]
    cid, duration, title = selected.get("cid"), selected.get("duration"), selected.get("part")
    if (
        type(cid) is not int or cid < 1 or type(duration) not in (int, float)
        or not math.isfinite(duration) or duration <= 0
        or not isinstance(title, str) or not title.strip()
    ):
        raise AudioSourceError("identity_unknown")
    try:
        data = await client.get_download_url(page_index=page - 1, cid=cid)
    except Exception:  # noqa: BLE001 -- redact foreign client messages at process boundary
        raise AudioSourceError("platform_request_failed") from None
    dash = data.get("dash") if isinstance(data, dict) else None
    if not isinstance(dash, dict):
        raise AudioSourceError("audio_stream_unavailable")
    milliseconds = data.get("timelength")
    if type(milliseconds) not in (int, float) or not math.isfinite(milliseconds):
        raise AudioSourceError("identity_duration_unknown")
    stream_duration = milliseconds / 1000
    if stream_duration <= 0 or abs(stream_duration - duration) > 2:
        raise AudioSourceError("identity_duration_mismatch")
    audio_streams = dash.get("audio")
    if not isinstance(audio_streams, list):
        raise AudioSourceError("audio_stream_unavailable")
    streams = [s for s in audio_streams if isinstance(s, dict)]
    if not streams:
        raise AudioSourceError("audio_stream_unavailable")
    # Public ordinary AAC only; no premium/Dolby/HiRes stream fallback.
    streams = [s for s in streams if s.get("id") in (30216, 30232, 30280)
               and type(s.get("bandwidth")) in (int, float)
               and math.isfinite(s["bandwidth"]) and s["bandwidth"] > 0]
    if not streams:
        raise AudioSourceError("audio_stream_unavailable")
    stream = min(streams, key=lambda s: s.get("bandwidth", float("inf")))
    url = stream.get("baseUrl") or stream.get("base_url")
    if not isinstance(url, str) or not _stream_identity(url, cid):
        raise AudioSourceError("identity_stream_cid_mismatch")
    return ResolvedAudio(
        bvid, page, cid, title, info.get("title", ""), float(duration),
        stream_duration, datetime.now(UTC).isoformat(), url,
    )


def anonymous_client(bvid: str) -> AudioClient:
    """No CLI auth module: never read saved credentials or browser cookie stores."""
    from bilibili_api import request_settings, video

    request_settings.set_wbi_retry_times(1)
    request_settings.set_enable_auto_buvid(False)
    request_settings.set_enable_bili_ticket(False)
    return video.Video(bvid=bvid)


async def download_audio(resolved: ResolvedAudio, destination: Path) -> dict:
    """One CDN request, no retry or alternate part; no signed URL persisted."""
    import aiohttp

    if not _stream_identity(resolved.stream_url, resolved.cid):
        raise AudioSourceError("identity_stream_cid_mismatch")
    if destination.exists():
        raise AudioSourceError("destination_exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".partial")
    if partial.exists():
        raise AudioSourceError("partial_exists")
    hasher, size = hashlib.sha256(), 0
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300), cookie_jar=aiohttp.DummyCookieJar()
        ) as session, session.get(resolved.stream_url, headers={
            "Referer": f"https://www.bilibili.com/video/{resolved.bvid}/?p={resolved.page}",
            "User-Agent": "Mozilla/5.0",
        }, allow_redirects=False) as response:
            if response.status != 200:
                raise AudioSourceError(f"download_http_{response.status}")
            expected_size = response.content_length
            with partial.open("xb") as output:
                async for chunk in response.content.iter_chunked(256 * 1024):
                    size += len(chunk)
                    if size > 256 * 1024 * 1024:
                        raise AudioSourceError("audio_size_limit")
                    output.write(chunk)
                    hasher.update(chunk)
            if size == 0 or (expected_size is not None and size != expected_size):
                raise AudioSourceError("incomplete_download")
        partial.rename(destination)
    except Exception as exc:
        partial.unlink(missing_ok=True)
        if isinstance(exc, AudioSourceError):
            raise
        raise AudioSourceError("download_failed") from None
    return {
        **resolved.proof(), "local_path": str(destination.resolve()),
        "audio_sha256": hasher.hexdigest(), "audio_bytes": size,
        "downloaded_at": datetime.now(UTC).isoformat(),
    }
