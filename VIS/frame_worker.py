"""Anonymous public-video frame worker. Only emits redacted proof metadata."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, "C:/Users/泽/Documents/VideoActionAgent/src")
from video_action_agent.audio_source import anonymous_client

FFMPEG = Path(
    "D:/VideoActionAgent-asr-pilot/venv/Lib/site-packages/"
    "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe"
)
_PTS_RE = re.compile(r"\bpts_time:([0-9]+(?:\.[0-9]+)?)")
_SIZE_RE = re.compile(r"\bs:([0-9]+)x([0-9]+)")
_CALLS = {"info": 0, "playurl": 0, "ffmpeg_attempts": 0}


class SafeFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _validate_request(payload: object) -> tuple[dict, float, list[float], Path]:
    if not isinstance(payload, dict):
        raise SafeFailure("invalid_request")
    source = payload.get("source")
    if not isinstance(source, dict):
        raise SafeFailure("invalid_request")
    bvid, page, cid = source.get("bvid"), source.get("page"), source.get("cid")
    if (
        not isinstance(bvid, str)
        or re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) is None
        or type(page) is not int
        or page < 1
        or type(cid) is not int
        or cid < 1
    ):
        raise SafeFailure("invalid_source")
    duration = payload.get("duration_seconds")
    if type(duration) not in (float, int) or not math.isfinite(duration) or duration <= 0:
        raise SafeFailure("invalid_duration")
    times = payload.get("requested_seconds")
    if not isinstance(times, list) or not 1 <= len(times) <= 3:
        raise SafeFailure("invalid_frame_count")
    requested = []
    for value in times:
        if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value < duration:
            raise SafeFailure("invalid_frame_time")
        requested.append(float(value))
    if len(set(requested)) != len(requested):
        raise SafeFailure("duplicate_frame_time")
    output_dir = payload.get("output_dir")
    if not isinstance(output_dir, str):
        raise SafeFailure("invalid_output")
    return {"bvid": bvid, "page": page, "cid": cid}, float(duration), requested, Path(output_dir)


def _stream_url(stream: dict, cid: int) -> str:
    url = stream.get("baseUrl") or stream.get("base_url")
    if not isinstance(url, str):
        raise SafeFailure("stream_unavailable")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or not (host == "bilivideo.com" or host.endswith(".bilivideo.com"))
        or str(cid) not in parsed.path.split("/")
    ):
        raise SafeFailure("stream_identity_mismatch")
    return url


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _extract_one(url: str, source: dict, at: float, index: int, directory: Path) -> dict:
    image_dir = directory / "frames"
    image_dir.mkdir(exist_ok=True)
    frame_path = image_dir / f"f{index:03d}.jpg"
    if frame_path.exists():
        raise SafeFailure("frame_destination_exists")
    command = [
        str(FFMPEG), "-hide_banner", "-nostdin", "-loglevel", "info",
        "-user_agent", "Mozilla/5.0",
        "-referer", f"https://www.bilibili.com/video/{source['bvid']}/?p={source['page']}",
        "-rw_timeout", "30000000", "-ss", str(at), "-copyts", "-i", url,
        "-an", "-frames:v", "1", "-vf", "showinfo", "-fps_mode", "vfr",
        "-q:v", "2", "-n", str(frame_path),
    ]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        raise SafeFailure("ffmpeg_start_failed") from None
    try:
        stderr = process.communicate(timeout=45)[1]
    except subprocess.TimeoutExpired:
        _terminate(process)
        raise SafeFailure("ffmpeg_timeout") from None
    if process.returncode != 0 or not frame_path.is_file():
        raise SafeFailure("frame_decode_failed")
    log = stderr.decode("utf-8", errors="replace")
    pts_matches = _PTS_RE.findall(log)
    size_matches = _SIZE_RE.findall(log)
    if not pts_matches or not size_matches:
        raise SafeFailure("frame_metadata_missing")
    actual = float(pts_matches[-1])
    width, height = (int(part) for part in size_matches[-1])
    data = frame_path.read_bytes()
    if (
        not math.isfinite(actual)
        or actual < 0
        or abs(actual - at) > 1.0
        or width < 1
        or height < 1
        or len(data) < 500
    ):
        raise SafeFailure("frame_metadata_invalid")
    return {
        "id": f"f{index:03d}",
        "requested_seconds": at,
        "actual_frame_pts_seconds": actual,
        "path": f"frames/f{index:03d}.jpg",
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": width,
        "height": height,
        "bytes": len(data),
    }


async def _capture(payload: dict) -> dict:
    source, requested_duration, times, directory = _validate_request(payload)
    client = anonymous_client(source["bvid"])
    _CALLS["info"] = 1
    info = await client.get_info()
    if not isinstance(info, dict) or info.get("bvid") != source["bvid"]:
        raise SafeFailure("source_identity_mismatch")
    pages = info.get("pages")
    matches = [item for item in pages if isinstance(item, dict) and item.get("page") == source["page"]] if isinstance(pages, list) else []
    if len(matches) != 1 or matches[0].get("cid") != source["cid"]:
        raise SafeFailure("source_identity_mismatch")
    source_page = matches[0]
    if (
        type(source_page.get("duration")) not in (int, float)
        or not math.isfinite(source_page["duration"])
        or abs(float(source_page["duration"]) - requested_duration) > 2.0
    ):
        raise SafeFailure("source_duration_mismatch")
    _CALLS["playurl"] = 1
    playurl = await client.get_download_url(page_index=source["page"] - 1, cid=source["cid"])
    if not isinstance(playurl, dict):
        raise SafeFailure("stream_unavailable")
    stream_duration = playurl.get("timelength")
    if type(stream_duration) not in (int, float) or not math.isfinite(stream_duration):
        raise SafeFailure("source_duration_mismatch")
    stream_duration /= 1000.0
    if abs(stream_duration - requested_duration) > 2.0:
        raise SafeFailure("source_duration_mismatch")
    dash = playurl.get("dash")
    videos = dash.get("video") if isinstance(dash, dict) else None
    eligible = [
        item for item in videos
        if isinstance(item, dict)
        and type(item.get("id")) is int
        and item["id"] <= 64
        and item.get("codecid") == 7
        and type(item.get("width")) is int
        and item["width"] > 0
        and type(item.get("height")) is int
        and item["height"] > 0
    ] if isinstance(videos, list) else []
    if not eligible:
        raise SafeFailure("ordinary_video_stream_unavailable")
    stream = max(eligible, key=lambda item: (item["height"], -int(item.get("bandwidth", 0))))
    url = _stream_url(stream, source["cid"])
    if not FFMPEG.is_file():
        raise SafeFailure("ffmpeg_unavailable")
    if (directory / "frames").exists():
        raise SafeFailure("frame_destination_exists")
    directory.mkdir(parents=True, exist_ok=True)
    frames = []
    for index, at in enumerate(times):
        _CALLS["ffmpeg_attempts"] += 1
        frames.append(_extract_one(url, source, at, index, directory))
    proof = {
        "schema_version": 1,
        "source": source,
        "duration_seconds": requested_duration,
        "acquisition": "live_anonymous_public_video",
        "browser_session": None,
        "selected_stream": {
            key: stream.get(key)
            for key in ("id", "width", "height", "codecid", "bandwidth", "mimeType")
        },
        "identity_basis": "current public info exact page CID and CDN path CID",
        "observed_at": datetime.now(UTC).isoformat(),
    }
    return {
        "schema_version": 1,
        "state": "available",
        "source": source,
        "frames": frames,
        "proof": proof,
        "observed_at": proof["observed_at"],
        "calls": dict(_CALLS),
        "automatic_retry": False,
    }


def main() -> int:
    for key in _CALLS:
        _CALLS[key] = 0
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        result = asyncio.run(_capture(payload))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except SafeFailure as exc:
        response = {
            "schema_version": 1,
            "state": "failed",
            "error_code": exc.code,
            "calls": dict(_CALLS),
            "automatic_retry": False,
        }
    except Exception:  # noqa: BLE001 - SDK exceptions may contain signed CDN URLs.
        response = {
            "schema_version": 1,
            "state": "failed",
            "error_code": "capture_failed",
            "calls": dict(_CALLS),
            "automatic_retry": False,
        }
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
