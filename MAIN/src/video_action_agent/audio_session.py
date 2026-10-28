"""Standard-library supervisor for a two-phase, anonymous SDK audio process."""

from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from video_action_agent.source_access import VideoRef

WORKER_PYTHON = Path("D:/VideoActionAgent-asr-pilot/audio-venv/Scripts/python.exe")
WORKER_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audio_source_session.py"
MAX_REPLY = 128 * 1024
IDENTITY_KEYS = (
    "bvid", "page", "cid", "part_title", "duration_seconds", "stream_duration_seconds",
    "access_mode", "browser_session_ref",
)


class AudioSessionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AudioSession:
    def __init__(self, ref: VideoRef, on_poll=None, cancel_check=None):
        self.ref = ref
        self.on_poll = on_poll
        self.cancel_check = cancel_check
        self._process = None
        self._replies = queue.Queue()
        self._proof = None
        self._state = "new"

    def _poll(self):
        if self.on_poll is not None:
            self.on_poll()
        if self.cancel_check is not None and self.cancel_check():
            raise AudioSessionError("cancelled")

    def _reader(self):
        try:
            while line := self._process.stdout.readline(MAX_REPLY + 1):
                self._replies.put(line)
                if len(line) > MAX_REPLY:
                    break
        except (OSError, ValueError):
            pass
        finally:
            self._replies.put(None)

    def _reply(self, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            self._poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AudioSessionError("timeout")
            try:
                line = self._replies.get(timeout=min(0.2, remaining))
            except queue.Empty:
                continue
            if line is None:
                raise AudioSessionError("worker_closed")
            if len(line) > MAX_REPLY or not line.endswith(b"\n"):
                raise AudioSessionError("invalid_reply")
            try:
                reply = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeError):
                raise AudioSessionError("invalid_reply") from None
            if not isinstance(reply, dict):
                raise AudioSessionError("invalid_reply")
            if "error_code" in reply:
                code = reply["error_code"]
                if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
                    code = "worker_failed"
                raise AudioSessionError(code)
            return reply

    def _validate_proof(self, proof):
        if not isinstance(proof, dict):
            raise AudioSessionError("invalid_reply")
        if (
            proof.get("bvid") != self.ref.bvid or type(proof.get("page")) is not int
            or proof["page"] != self.ref.page or type(proof.get("cid")) is not int
            or proof["cid"] < 1 or not isinstance(proof.get("part_title"), str)
            or not proof["part_title"].strip()
            or proof.get("access_mode") != "anonymous_public_client"
            or proof.get("browser_session_ref") is not None
        ):
            raise AudioSessionError("identity_mismatch")
        for key in ("duration_seconds", "stream_duration_seconds"):
            value = proof.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise AudioSessionError("identity_duration_unknown")
        if abs(proof["duration_seconds"] - proof["stream_duration_seconds"]) > 2:
            raise AudioSessionError("identity_duration_mismatch")

    def resolve(self, timeout: float = 60):
        if self._state != "new":
            raise AudioSessionError("invalid_state")
        self._state = "resolving"
        try:
            self._process = subprocess.Popen(
                [str(WORKER_PYTHON), str(WORKER_SCRIPT), "--bvid", self.ref.bvid,
                 "--page", str(self.ref.page)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            threading.Thread(target=self._reader, daemon=True).start()
            reply = self._reply(timeout)
            proof = reply.get("proof")
            self._validate_proof(proof)
            self._proof = dict(proof)
            self._state = "resolved"
            return dict(self._proof)
        except OSError:
            self.close()
            raise AudioSessionError("worker_unavailable") from None
        except BaseException:
            self.close()
            raise

    def download(self, destination: Path, timeout: float = 330):
        if self._state != "resolved":
            raise AudioSessionError("invalid_state")
        self._state = "downloading"
        destination = Path(destination).resolve()
        verified = False
        can_cleanup = False
        try:
            self._poll()
            if destination.exists() or destination.with_suffix(destination.suffix + ".partial").exists():
                raise AudioSessionError("destination_exists")
            can_cleanup = True
            command = json.dumps({"action": "download", "destination": str(destination)}) + "\n"
            self._process.stdin.write(command.encode("utf-8"))
            self._process.stdin.flush()
            self._process.stdin.close()
            reply = self._reply(timeout)
            self._validate_proof(reply)
            if any(reply.get(key) != self._proof.get(key) for key in IDENTITY_KEYS):
                raise AudioSessionError("identity_mismatch")
            if (not isinstance(reply.get("local_path"), str)
                or Path(reply["local_path"]).resolve() != destination):
                raise AudioSessionError("output_path_mismatch")
            claimed_hash, size = reply.get("audio_sha256"), reply.get("audio_bytes")
            if (
                not isinstance(claimed_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", claimed_hash)
                or type(size) is not int or size <= 0 or size > 256 * 1024 * 1024
                or not destination.is_file() or destination.stat().st_size != size
            ):
                raise AudioSessionError("audio_integrity_mismatch")
            hasher = hashlib.sha256()
            with destination.open("rb") as audio:
                while chunk := audio.read(1024 * 1024):
                    self._poll()
                    hasher.update(chunk)
            if hasher.hexdigest() != claimed_hash:
                raise AudioSessionError("audio_integrity_mismatch")
            verified = True
            return reply
        except OSError:
            raise AudioSessionError("download_protocol_failed") from None
        finally:
            self.close()
            if can_cleanup and not verified:
                destination.unlink(missing_ok=True)
                destination.with_suffix(destination.suffix + ".partial").unlink(missing_ok=True)

    def close(self):
        process = self._process
        if process is not None:
            if process.poll() is None:
                if os.name == "nt":
                    try:
                        subprocess.run(
                            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            check=False, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
        self._state = "closed"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
