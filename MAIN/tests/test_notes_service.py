import json
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_source_candidate_bundle import candidate

from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_service import NotesService, NotesServiceError


class Provider:
    model_id = "controlled-provider"

    def __init__(self, started=None, release=None, fail=False):
        self.started, self.release, self.fail = started, release, fail
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if self.started:
            self.started.set()
            assert self.release.wait(3)
        if self.fail:
            raise RuntimeError("private upstream detail")
        body = json.loads(user)
        return json.dumps({"source_id": body["source_id"], "request_id": body["request_id"],
                           "title": "课程笔记", "sections": [{"title": "关键概念",
                           "items": [{"text": "来源支持的概念", "segment_indices": [1]}]}],
                           "unverified": []}, ensure_ascii=False)


def setup(tmp_path, provider):
    manager = NotesService("alice", tmp_path / "notes", lambda: provider,
                           saved_bundle=candidate(tmp_path))
    client = TestClient(create_ui_app(object(), {"a": "alice", "b": "bob"},
                                     source_modes=(), notes_service=manager),
                        base_url="http://127.0.0.1")
    body = {"request_id": str(uuid4()), "source_id": "saved", "user_request": "详细课程笔记",
            "allow_model_text": True}
    return manager, client, body


def test_notes_permission_binding_idempotence_and_persistent_read(tmp_path):
    provider = Provider()
    manager, client, body = setup(tmp_path, provider)
    headers = {"Authorization": "Bearer a"}
    assert client.post("/app/notes", json=body).status_code == 401
    assert client.post("/app/notes", json=body, headers={"Authorization": "Bearer b"}).status_code == 404
    assert client.post("/app/notes", json={**body, "allow_model_text": False},
                       headers=headers).status_code == 403
    assert client.post("/app/notes", json={**body, "source_id": "../private"},
                       headers=headers).status_code == 409
    assert provider.calls == 0
    source = client.get("/app/notes/sources", headers=headers).json()["sources"][0]
    assert source["page"] == 42 and source["source_id"] == "saved"
    job = client.post("/app/notes", json=body, headers=headers).json()
    manager.wait(job["job_id"], 3)
    assert client.get(f"/app/notes/{job['job_id']}", headers=headers).json()["state"] == "available"
    assert client.post("/app/notes", json=body, headers=headers).status_code == 200
    assert client.post("/app/notes", json={**body, "user_request": "其他请求"},
                       headers=headers).status_code == 409
    result = client.get(f"/app/notes/{job['job_id']}/candidate", headers=headers)
    assert result.status_code == 200 and result.json()["completed"] is False
    assert result.json()["candidate"]["source_ref"]["page"] == 42
    assert "p=42" in result.json()["markdown"] and provider.calls == 1
    restarted = NotesService("alice", tmp_path / "notes", lambda: provider)
    assert restarted.candidate(job["job_id"]) == result.json()
    other = NotesService("bob", tmp_path / "notes", lambda: provider)
    with pytest.raises(NotesServiceError, match="notes_candidate_unavailable"):
        other.candidate(job["job_id"])
    (tmp_path / "notes" / job["job_id"] / "notes.md").write_text("changed", encoding="utf-8")
    with pytest.raises(NotesServiceError, match="notes_candidate_unavailable"):
        restarted.candidate(job["job_id"])


def test_notes_cancellation_discards_late_model_and_failure_never_retries(tmp_path):
    started, release = threading.Event(), threading.Event()
    provider = Provider(started, release)
    manager, client, body = setup(tmp_path, provider)
    headers = {"Authorization": "Bearer a"}
    job = client.post("/app/notes", json=body, headers=headers).json()
    assert started.wait(3)
    client.post(f"/app/notes/{job['job_id']}/cancel", headers=headers)
    release.set()
    manager.wait(job["job_id"], 3)
    assert client.get(f"/app/notes/{job['job_id']}", headers=headers).json()["state"] == "canceled"
    assert client.get(f"/app/notes/{job['job_id']}/candidate", headers=headers).status_code == 409
    assert not (tmp_path / "notes" / job["job_id"]).exists()
    provider.started = None
    provider.fail = True
    body["request_id"] = str(uuid4())
    job = client.post("/app/notes", json=body, headers=headers).json()
    manager.wait(job["job_id"], 3)
    failed = client.get(f"/app/notes/{job['job_id']}", headers=headers).json()
    assert failed["state"] == "failed" and failed["error_code"] == "notes_failed"
    assert "private" not in json.dumps(failed) and provider.calls == 2
    assert not (tmp_path / "notes" / job["job_id"]).exists()
