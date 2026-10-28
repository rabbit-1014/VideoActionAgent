"""Standalone explicit-part audio probe; writes only local intake evidence."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_action_agent.audio_source import (
    AudioSourceError,
    anonymous_client,
    download_audio,
    resolve_audio,
)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--page", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--allow-audio-download", action="store_true")
    args = parser.parse_args()
    if not args.allow_audio_download:
        parser.error("explicit --allow-audio-download required")
    started = time.monotonic()
    destination = args.output_dir / f"{args.bvid}-p{args.page}.m4a"
    try:
        resolved = await resolve_audio(args.bvid, args.page, anonymous_client(args.bvid))
        print(json.dumps(resolved.proof(), ensure_ascii=False), flush=True)
        result = await download_audio(resolved, destination)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["status"] = "downloaded_unverified_media_duration"
    except AudioSourceError as exc:
        print(json.dumps({"status": "failed", "error_code": str(exc)}, ensure_ascii=False))
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    evidence = args.output_dir / f"{args.bvid}-p{args.page}-audio-evidence.json"
    evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
