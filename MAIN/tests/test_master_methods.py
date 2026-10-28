"""Saved masters reuse generation/storage fences without recapturing sources."""

import json
import threading
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_source_candidate_bundle import candidate

from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_service import NotesService, NotesServiceError


class MethodProvider:
    model_id = "controlled-method-provider"

    def __init__(self, started=None, release=None):
        self.calls = 0
        self.started, self.release = started, release

    def complete(self, system, user):
        self.calls += 1
        if self.started:
            self.started.set()
            assert self.release.wait(3)
        body = json.loads(user)
        first = body["segments"][0]
        evidence = {"text": "按原片段的方法处理", "segment_indices": [1],
                    "start": first["start"], "end": first["end"]}
        return json.dumps({"user_request": body["user_request"],
                           "source_ref": body["source_ref"], "payload_hash": body["payload_hash"],
                           "methods": [{"title": "视频演示的方法", "applicability": "具备演示的输入",
                                        "steps": [{"order": 1, **evidence}],
                                        "checkpoints": [evidence]}], "unverified": []},
                          ensure_ascii=False)


def setup(tmp_path, provider):
    bundle = candidate(tmp_path)
    manager = NotesService("alice", tmp_path / "results", lambda: provider, saved_bundle=bundle)
    client = TestClient(create_ui_app(object(), {"a": "alice", "b": "bob"},
                                     source_modes=(), notes_service=manager),
                        base_url="http://127.0.0.1")
    body = {"request_id": str(uuid4()), "source_id": "saved", "user_request": "按本视频的方法做事",
            "allow_model_text": True, "output_kind": "method_list"}
    return manager, client, body, bundle


def test_master_method_list_roundtrip_preserves_source_and_owner(tmp_path):
    provider = MethodProvider()
    manager, client, body, bundle = setup(tmp_path, provider)
    original = deepcopy(bundle)
    headers = {"Authorization": "Bearer a"}
    response = client.post("/app/notes", json=body, headers=headers)
    assert response.status_code == 200
    job = response.json()
    manager.wait(job["job_id"], 3)
    result = client.get(f"/app/notes/{job['job_id']}/candidate", headers=headers)
    assert result.status_code == 200
    value = result.json()
    draft = value["candidate"]
    assert draft["kind"] == "method_list" and value["completed"] is False
    assert draft["source_ref"]["provenance"]["timestamp_kind"] == "vad_segment"
    assert draft["source_ref"]["cid"] == bundle["source_envelope"]["source_evidence"]["cid"]
    assert "剪映" not in value["markdown"] and "方法清单" in value["markdown"]
    assert "现行" in draft["unverified"][0]
    assert bundle == original and provider.calls == 1
    assert client.post("/app/notes", json=body, headers=headers).status_code == 200
    assert client.post("/app/notes", json={**body, "output_kind": "structured_notes"},
                       headers=headers).status_code == 409
    assert provider.calls == 1
    restarted = NotesService("alice", tmp_path / "results", lambda: None)
    assert restarted.candidate(job["job_id"]) == value
    assert restarted.get(job["job_id"])["output_kind"] == "method_list"
    other = NotesService("bob", tmp_path / "results", lambda: None)
    with pytest.raises(NotesServiceError, match="notes_candidate_unavailable"):
        other.candidate(job["job_id"])
    method_file = tmp_path / "results" / job["job_id"] / "method-list.md"
    assert method_file.read_text(encoding="utf-8") == value["markdown"]
    method_file.write_text("changed", encoding="utf-8")
    with pytest.raises(NotesServiceError, match="notes_candidate_unavailable"):
        restarted.candidate(job["job_id"])


def test_method_permission_type_and_cancellation_keep_existing_fences(tmp_path):
    started, release = threading.Event(), threading.Event()
    provider = MethodProvider(started, release)
    manager, client, body, _ = setup(tmp_path, provider)
    headers = {"Authorization": "Bearer a"}
    assert client.post("/app/notes", json=body).status_code == 401
    assert client.post("/app/notes", json=body, headers={"Authorization": "Bearer b"}).status_code == 404
    assert client.post("/app/notes", json={**body, "allow_model_text": False},
                       headers=headers).status_code == 403
    assert client.post("/app/notes", json={**body, "output_kind": "unknown"},
                       headers=headers).status_code == 422
    assert provider.calls == 0
    response = client.post("/app/notes", json=body, headers=headers)
    assert response.status_code == 200
    job = response.json()
    try:
        assert started.wait(3)
        assert client.post(f"/app/notes/{job['job_id']}/cancel", headers=headers).status_code == 200
    finally:
        release.set()
    manager.wait(job["job_id"], 3)
    assert manager.get(job["job_id"])["state"] == "canceled"
    assert not (tmp_path / "results" / job["job_id"]).exists()
    assert client.get(f"/app/notes/{job['job_id']}/candidate", headers=headers).status_code == 409


def test_known_provider_failure_is_visible_without_automatic_retry(tmp_path):
    from video_action_agent.deepseek_provider import ProviderError

    class FailedProvider(MethodProvider):
        def complete(self, system, user):
            self.calls += 1
            raise ProviderError("request_failed")

    provider = FailedProvider()
    manager, client, body, _ = setup(tmp_path, provider)
    response = client.post("/app/notes", json=body, headers={"Authorization": "Bearer a"})
    assert response.status_code == 200
    job = response.json()
    manager.wait(job["job_id"], 3)
    assert manager.get(job["job_id"])["error_code"] == "request_failed"
    assert provider.calls == 1 and not (tmp_path / "results" / job["job_id"]).exists()
