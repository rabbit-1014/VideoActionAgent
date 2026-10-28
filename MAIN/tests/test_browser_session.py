import json
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_action_agent.browser_session import LocalOpenCliSessionProbe, SessionProbeError
from video_action_agent.models import DispatchSessionProof


@pytest.fixture
def local_opencli():
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        entry = Path(temp_dir) / "node_modules" / "opencli" / "dist" / "src" / "main.js"
        entry.parent.mkdir(parents=True)
        entry.write_text("", encoding="utf-8")
        (entry.parents[2] / "package.json").write_text(
            json.dumps({"version": "1.8.6"}), encoding="utf-8"
        )
        yield "node", entry


def reply(payload, returncode: int = 0) -> SimpleNamespace:
    output = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(returncode=returncode, stdout=output.encode(), stderr=b"")


CONNECTED = """opencli v1.8.6 doctor (node v24.14.1)
[OK] Daemon: running on port 19825 (v1.8.6)
[OK] Extension: connected (v1.0.24)
[OK] Connectivity: connected in 0.3s
"""


AUTH = [{"site": "bilibili", "status": "logged_in", "logged_in": True, "error": ""}]


def test_session_probe_fails_closed_when_doctor_reports_disconnected(local_opencli):
    node, entry = local_opencli
    calls = iter([reply(CONNECTED.replace("Connectivity: connected", "Connectivity: failed"))])

    probe = LocalOpenCliSessionProbe(node, entry, runner=lambda *a, **k: next(calls))

    with pytest.raises(SessionProbeError, match="connection_unverified"):
        probe.observe()


def test_session_probe_fails_closed_when_bilibili_is_not_logged_in(local_opencli):
    node, entry = local_opencli
    calls = iter([
        reply(CONNECTED),
        reply([{**AUTH[0], "status": "not_logged_in", "logged_in": False}]),
    ])

    probe = LocalOpenCliSessionProbe(node, entry, runner=lambda *a, **k: next(calls))

    with pytest.raises(SessionProbeError, match="authentication_unverified"):
        probe.observe()


def test_session_probe_returns_runtime_compatible_proof_only_after_both_checks(local_opencli):
    node, entry = local_opencli
    calls = iter([
        reply(CONNECTED),
        reply(AUTH),
    ])

    probe = LocalOpenCliSessionProbe(
        node, entry, runner=lambda *a, **k: next(calls), clock=lambda: "2026-09-25T12:00:00+08:00"
    )
    proof = probe.observe()

    assert proof == {
        "schema_version": 1,
        "observed_at": "2026-09-25T12:00:00+08:00",
        "adapter_version": "1.8.6",
        "browser_session_ref": proof["browser_session_ref"],
        "observed_connection_state": "verified",
        "observation_command": "opencli doctor; opencli auth status --site bilibili -f json",
        "observation_return": "doctor bridge=connected v1.0.24 connectivity=connected; bilibili auth logged_in=true",
    }
    assert proof["browser_session_ref"].startswith("opencli-")
    assert DispatchSessionProof.model_validate(proof).observed_connection_state == "verified"


def test_session_probe_fails_closed_on_subprocess_timeout(local_opencli):
    node, entry = local_opencli

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("opencli doctor", 10)

    probe = LocalOpenCliSessionProbe(node, entry, runner=timeout)

    with pytest.raises(SessionProbeError, match="probe_timeout"):
        probe.observe()
