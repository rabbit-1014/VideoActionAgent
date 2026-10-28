"""Two-phase child: resolve proof, wait for one explicit download command, exit."""

import argparse
import asyncio
import contextlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video_action_agent.audio_source import (
    AudioSourceError,
    anonymous_client,
    download_audio,
    resolve_audio,
)


async def serve(bvid, page, commands, replies):
    def send(payload):
        replies.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
        replies.flush()

    try:
        # Library stdout and diagnostics never enter protocol or persist signed URLs.
        with contextlib.redirect_stdout(sys.stderr):
            resolved = await resolve_audio(bvid, page, anonymous_client(bvid))
        send({"proof": resolved.proof()})
        line = commands.readline(32769)
        if not line:
            return 0
        if len(line) > 32768:
            raise AudioSourceError("invalid_command")
        command = json.loads(line)
        if (
            not isinstance(command, dict) or command.get("action") != "download"
            or not isinstance(command.get("destination"), str)
        ):
            raise AudioSourceError("invalid_command")
        destination = Path(command["destination"])
        if not destination.is_absolute():
            raise AudioSourceError("invalid_destination")
        with contextlib.redirect_stdout(sys.stderr):
            result = await download_audio(resolved, destination)
        send(result)
        return 0
    except AudioSourceError as exc:
        code = str(exc)
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
            code = "worker_failed"
        send({"error_code": code})
        return 1
    except Exception:  # noqa: BLE001 -- redact all foreign failures at IPC boundary
        send({"error_code": "worker_failed"})
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bvid", required=True)
    parser.add_argument("--page", required=True, type=int)
    args = parser.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(serve(args.bvid, args.page, sys.stdin, sys.stdout))


if __name__ == "__main__":
    sys.exit(main())
