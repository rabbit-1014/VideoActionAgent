import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_source_candidate_bundle import api, candidate

SOURCE = "https://www.bilibili.com/video/BV1AJen6SEVQ/?p=42"


def test_recorded_source_survives_cold_read_without_reacquisition(tmp_path):
    from video_action_agent.source_capture import SourceCapture

    calls = []
    bundle = candidate(tmp_path)
    assert bundle["acquisition"] == "recorded_audio"
    manager = SourceCapture("alice", tmp_path / "saved", capture_fn=lambda *a, **k: calls.append(True))
    identity = str(uuid4())
    proof = bundle["source_envelope"]["source_evidence"]
    api().save_bundle(bundle, manager.output_root / identity)
    manager._store.save(identity, {"job_id": identity, "state": "available", "stage": None,
        "bvid": proof["bvid"], "page": proof["page"], "error_code": None},
        {"bvid": proof["bvid"], "page": proof["page"], "allow_local_audio_read": True})
    manager.close()
    restored = SourceCapture("alice", tmp_path / "saved", capture_fn=lambda *a, **k: calls.append(True))
    assert restored.bundle(identity) == bundle
    assert restored.history()["jobs"][0]["result_available"] is True
    assert restored.bundle(identity)["acquisition"] == "recorded_audio"
    assert calls == []
    restored.close()


def service(tmp_path, capture):
    from video_action_agent.local_ui_app import create_ui_app
    from video_action_agent.source_capture import SourceCapture

    manager = SourceCapture("alice", tmp_path / "outputs", capture_fn=capture)
    client = TestClient(create_ui_app(object(), {"a": "alice", "b": "bob"},
                                     source_modes=(), source_capture=manager),
                        base_url="http://127.0.0.1")
    return manager, client


def test_capture_permission_and_owner_rejections_do_not_read(tmp_path):
    calls = []
    manager, client = service(tmp_path, lambda *a, **k: calls.append(True))
    body = {"request_id": str(uuid4()), "source": SOURCE, "allow_local_audio_read": True}
    assert client.post("/app/intake", json=body).status_code == 401
    assert client.post("/app/intake", json=body, headers={"Authorization": "Bearer b"}).status_code == 404
    body["allow_local_audio_read"] = False
    assert client.post("/app/intake", json=body, headers={"Authorization": "Bearer a"}).status_code == 403
    body["allow_local_audio_read"] = True
    body["source"] = "https://example.com/private"
    assert client.post("/app/intake", json=body, headers={"Authorization": "Bearer a"}).status_code == 422
    assert calls == []
    manager.close()


def test_capture_is_idempotent_precise_and_publishes_only_validated_candidate(tmp_path):
    calls, started, release = [], threading.Event(), threading.Event()
    bundle = candidate(tmp_path)
    bundle["acquisition"] = "live_anonymous_audio"

    def capture(source, output, **kwargs):
        calls.append(source)
        kwargs["on_stage"]("transcribing")
        started.set()
        assert release.wait(3)
        api().save_bundle(bundle, output)

    manager, client = service(tmp_path, capture)
    headers = {"Authorization": "Bearer a"}
    job_id = str(uuid4())
    body = {"request_id": job_id, "source": SOURCE, "allow_local_audio_read": True}
    assert client.post("/app/intake", json=body, headers=headers).status_code == 200
    assert started.wait(3)
    assert client.post("/app/intake", json=body, headers=headers).json()["job_id"] == job_id
    changed = {**body, "source": SOURCE.replace("42", "41")}
    assert client.post("/app/intake", json=changed, headers=headers).status_code == 409
    assert client.get("/app/intake", headers=headers).json()["job"]["stage"] == "transcribing"
    release.set()
    manager.wait(job_id, 3)
    result = client.get(f"/app/intake/{job_id}/candidate", headers=headers)
    assert result.status_code == 200 and result.json()["source_ref"]["page"] == 42
    assert result.json()["acquisition"] == "live_anonymous_audio"
    assert result.json()["formal_task"] is False and result.json()["completed"] is False
    assert "outputs" not in result.text
    assert client.get(f"/app/intake/{job_id}/candidate", headers={"Authorization": "Bearer b"}).status_code == 404
    assert calls == [SOURCE.replace("/?", "?")]
    manager.close()


def test_cancel_discards_late_candidate_and_failures_do_not_retry(tmp_path):
    from video_action_agent.content_intake import IntakeError

    started, release = threading.Event(), threading.Event()
    calls = []
    bundle = candidate(tmp_path)
    bundle["acquisition"] = "live_anonymous_audio"

    def capture(source, output, **kwargs):
        calls.append(source)
        if len(calls) == 2:
            raise IntakeError("transcription_timeout")
        started.set()
        assert release.wait(3)
        api().save_bundle(bundle, output)  # Deliberately late despite cancellation.

    manager, client = service(tmp_path, capture)
    headers = {"Authorization": "Bearer a"}
    job_id = str(uuid4())
    body = {"request_id": job_id, "source": SOURCE, "allow_local_audio_read": True}
    client.post("/app/intake", json=body, headers=headers)
    assert started.wait(3)
    assert client.post(f"/app/intake/{job_id}/cancel", headers=headers).json()["state"] == "cancel_requested"
    release.set()
    manager.wait(job_id, 3)
    assert client.get(f"/app/intake/{job_id}", headers=headers).json()["state"] == "canceled"
    assert client.get(f"/app/intake/{job_id}/candidate", headers=headers).status_code == 409
    client.post("/app/intake", json=body, headers=headers)
    failure_id = str(uuid4())
    client.post("/app/intake", json={**body, "request_id": failure_id}, headers=headers)
    manager.wait(failure_id, 3)
    failure = client.get(f"/app/intake/{failure_id}", headers=headers).json()
    assert failure["state"] == "failed" and failure["error_code"] == "transcription_timeout"
    assert failure["result_available"] is False and len(calls) == 2
    manager.close()


def test_capture_cancel_at_publication_does_not_save(monkeypatch, tmp_path):
    from test_content_intake import fixtures

    from video_action_agent.content_intake import IntakeError, capture

    calls, _ = fixtures(monkeypatch, tmp_path)
    with pytest.raises(IntakeError, match="cancelled"):
        capture(SOURCE, tmp_path / "out", allow_local_audio_read=True,
                cancel_check=lambda: "build" in calls)
    assert "build" in calls and "save" not in calls and calls[-1] == "close"
