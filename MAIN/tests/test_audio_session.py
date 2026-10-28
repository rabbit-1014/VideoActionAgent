import json
import subprocess
import sys

import pytest

from video_action_agent.audio_session import AudioSession, AudioSessionError
from video_action_agent.source_access import VideoRef

PROOF = {"bvid": "BV1AJen6SEVQ", "page": 42, "cid": 41961066345,
         "part_title": "target", "duration_seconds": 1984.0,
         "stream_duration_seconds": 1983.727, "access_mode": "anonymous_public_client",
         "browser_session_ref": None}


def stub(monkeypatch, program):
    original = subprocess.Popen
    processes = []

    def launch(command, **kwargs):
        if command[0] == "taskkill":
            return original(command, **kwargs)
        assert command[1].endswith("audio_source_session.py")
        process = original([sys.executable, "-u", "-c", program], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("video_action_agent.audio_session.subprocess.Popen", launch)
    return processes


def test_resolve_waits_for_explicit_download_then_close(monkeypatch, tmp_path):
    marker = tmp_path / "downloaded"
    program = (
        f"import sys,json,pathlib;print({json.dumps({'proof': PROOF})!r},flush=True);"
        f"line=sys.stdin.readline();pathlib.Path({str(marker)!r}).write_text(line)"
    )
    processes = stub(monkeypatch, program)
    session = AudioSession(VideoRef("BV1AJen6SEVQ", 42))
    assert session.resolve()["cid"] == 41961066345
    assert not marker.exists()
    session.close()
    assert processes[0].poll() is not None
    assert not marker.exists()


def test_wrong_part_refused_and_worker_closed(monkeypatch):
    proof = {**PROOF, "page": 1}
    processes = stub(monkeypatch, f"import time;print({json.dumps({'proof': proof})!r},"
                     "flush=True);time.sleep(30)")
    session = AudioSession(VideoRef("BV1AJen6SEVQ", 42))
    with pytest.raises(AudioSessionError, match="identity_mismatch"):
        session.resolve()
    assert processes[0].poll() is not None


def test_cancel_cleans_waiting_process(monkeypatch):
    processes = stub(monkeypatch, "import time;time.sleep(30)")
    session = AudioSession(VideoRef("BV1AJen6SEVQ", 42), cancel_check=lambda: True)
    with pytest.raises(AudioSessionError) as caught:
        session.resolve()
    assert caught.value.code == "cancelled"
    assert processes[0].poll() is not None


@pytest.mark.parametrize("tamper_hash", [False, True])
def test_download_hash_recomputed_and_one_command_only(monkeypatch, tmp_path, tamper_hash):
    program = f'''
import json,sys,pathlib,hashlib
proof={PROOF!r}
print(json.dumps({{"proof":proof}}),flush=True)
command=json.loads(sys.stdin.readline())
path=pathlib.Path(command["destination"])
path.write_bytes(b"audio bytes")
print(json.dumps({{**proof,"local_path":str(path),"audio_bytes":11,
"audio_sha256":{'"0"*64' if tamper_hash else 'hashlib.sha256(b"audio bytes").hexdigest()'}}}),flush=True)
'''
    stub(monkeypatch, program)
    session = AudioSession(VideoRef("BV1AJen6SEVQ", 42))
    session.resolve()
    destination = tmp_path / "audio.m4a"
    if tamper_hash:
        with pytest.raises(AudioSessionError, match="audio_integrity_mismatch"):
            session.download(destination)
        assert not destination.exists()
    else:
        result = session.download(destination)
        assert result["audio_bytes"] == 11
    with pytest.raises(AudioSessionError, match="invalid_state"):
        session.download(tmp_path / "other.m4a")
