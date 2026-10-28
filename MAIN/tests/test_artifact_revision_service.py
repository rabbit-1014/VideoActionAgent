import hashlib
import json
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_notes_service import Provider
from test_source_candidate_bundle import candidate

from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_service import NotesService, NotesServiceError


class RevisionProvider(Provider):
    def __init__(self):
        super().__init__()
        self.inputs = []

    def complete(self, system, user):
        self.inputs.append(json.loads(user))
        result = json.loads(super().complete(system, user))
        if "revision_context" in self.inputs[-1]:
            result["title"] = "按修改要求重排的笔记"
        return json.dumps(result, ensure_ascii=False)


def setup_revision(tmp_path):
    provider = RevisionProvider()
    providers = [provider]
    manager = NotesService("alice", tmp_path / "results", lambda: providers[-1],
                           saved_bundle=candidate(tmp_path))
    original = str(uuid4())
    manager.start(original, "saved", "整理适合新手的课程笔记", allow_model_text=True)
    manager.wait(original, 3)
    original_value = manager.candidate(original)
    client = TestClient(create_ui_app(object(), {"a": "alice", "b": "bob"},
                                     source_modes=(), notes_service=manager),
                        base_url="http://127.0.0.1")
    return manager, client, provider, providers, original, original_value


def body(value):
    return {"request_id": str(uuid4()),
            "expected_parent_fingerprint": value["revision"]["record_fingerprint"],
            "instruction": "把步骤集中到一起，保留材料未核实处。", "allow_model_text": True}


def files(root, identity):
    return {path.name: path.read_bytes() for path in (root / identity).iterdir()}


def test_revision_consumes_original_and_restores_both_immutable_versions(tmp_path):
    manager, client, provider, _, original, old = setup_revision(tmp_path)
    payload = body(old)
    before = files(manager.output_root, original)
    response = client.post(f"/app/notes/{original}/revisions", json=payload,
                           headers={"Authorization": "Bearer a"})
    assert response.status_code == 200
    identity = response.json()["job_id"]
    manager.wait(identity, 3)
    revised = manager.candidate(identity)
    context = provider.inputs[-1]["revision_context"]
    assert context["previous_candidate"] == old["candidate"]
    assert context["instruction"] == payload["instruction"]
    assert context["original_user_request"] == old["candidate"]["user_request"]
    assert revised["candidate"]["title"] == "按修改要求重排的笔记"
    assert revised["candidate"]["source_ref"] == old["candidate"]["source_ref"]
    assert revised["candidate"]["user_request"] == old["candidate"]["user_request"]
    assert revised["revision"]["root_job_id"] == original
    assert revised["revision"]["parent_job_id"] == original
    assert revised["revision"]["version"] == 2
    assert revised["formal_task"] is False and revised["completed"] is False
    assert files(manager.output_root, original) == before
    assert client.post(f"/app/notes/{original}/revisions", json=payload,
                       headers={"Authorization": "Bearer a"}).json()["job_id"] == identity
    assert provider.calls == 2
    restarted = NotesService("alice", manager.output_root, lambda: pytest.fail("No new model call"))
    assert restarted.candidate(original) == old
    assert restarted.candidate(identity) == revised
    assert restarted._bundle("artifact:" + identity) == manager._bundle("saved")
    versions = {row["job_id"]: row["version"] for row in restarted.history()["results"]}
    assert versions == {original: 1, identity: 2}
    # A retry after restart still refers to the same already-published request.
    assert restarted.revise(original, payload["request_id"], payload["expected_parent_fingerprint"],
                            payload["instruction"], allow_model_text=True)["job_id"] == identity


def test_revision_rejects_unpermitted_foreign_stale_or_tampered_lineage(tmp_path):
    manager, client, provider, _, original, old = setup_revision(tmp_path)
    payload = body(old)
    url = f"/app/notes/{original}/revisions"
    headers = {"Authorization": "Bearer a"}
    assert client.post(url, json=payload).status_code == 401
    assert client.post(url, json=payload, headers={"Authorization": "Bearer b"}).status_code == 404
    assert client.post(url, json={**payload, "allow_model_text": False}, headers=headers).status_code == 403
    assert client.post(url, json={**payload, "allow_model_text": 1}, headers=headers).status_code == 422
    assert client.post(url, json={**payload, "expected_parent_fingerprint": "0" * 64},
                       headers=headers).status_code == 409
    assert provider.calls == 1 and len(manager.history()["results"]) == 1
    identity = client.post(url, json=payload, headers=headers).json()["job_id"]
    manager.wait(identity, 3)
    assert client.post(url, json=body(old), headers=headers).json()["detail"] == "revision_parent_superseded"
    assert client.post(url, json={**payload, "instruction": "不同要求"},
                       headers=headers).json()["detail"] == "notes_request_conflict"
    assert provider.calls == 2
    assert NotesService("bob", manager.output_root, lambda: None).history()["results"] == []
    # Rehashing the local file manifest cannot bypass semantic/signature lineage checks.
    path = manager.output_root / identity / "candidate.json"
    original_bytes = path.read_bytes()
    original_manifest = (path.parent / "manifest.json").read_bytes()
    record = json.loads(path.read_text(encoding="utf-8"))
    record["revision"]["root_job_id"] = str(uuid4())
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    manifest_path = path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(NotesServiceError, match="notes_candidate_unavailable"):
        NotesService("alice", manager.output_root, lambda: None).candidate(identity)
    path.write_text("[]")
    assert client.get(f"/app/notes/{identity}/candidate", headers=headers).status_code == 404
    path.write_bytes(original_bytes)
    manifest_path.write_text(json.dumps(["candidate.json", "notes.md"]))
    assert client.get(f"/app/notes/{identity}/candidate", headers=headers).status_code == 404
    manifest_path.write_bytes(original_manifest)
    assert manager.candidate(original) == old


def test_failed_canceled_and_interrupted_revisions_preserve_head_and_do_not_replay(tmp_path):
    manager, _, _, providers, original, old = setup_revision(tmp_path)
    failed = body(old)
    failing = Provider(fail=True)
    providers.append(failing)
    manager.revise(original, failed["request_id"], failed["expected_parent_fingerprint"],
                   failed["instruction"], allow_model_text=True)
    manager.wait(failed["request_id"], 3)
    assert manager.get(failed["request_id"])["state"] == "failed"
    # Broken failed-attempt history must not erase its version reservation.
    journal = manager.output_root / ".jobs" / (failed["request_id"] + ".json")
    retained = journal.read_bytes()
    journal.write_text("{")
    denied = body(old)
    with pytest.raises(NotesServiceError, match="revision_history_incomplete"):
        manager.revise(original, denied["request_id"], denied["expected_parent_fingerprint"],
                       denied["instruction"], allow_model_text=True)
    assert failing.calls == 1
    journal.write_bytes(retained)
    started, release = threading.Event(), threading.Event()
    slow = Provider(started=started, release=release)
    providers.append(slow)
    canceled = body(old)
    manager.revise(original, canceled["request_id"], canceled["expected_parent_fingerprint"],
                   canceled["instruction"], allow_model_text=True)
    assert started.wait(2)
    manager.cancel(canceled["request_id"])
    release.set()
    manager.wait(canceled["request_id"], 3)
    assert manager.get(canceled["request_id"])["state"] == "canceled"
    assert not (manager.output_root / canceled["request_id"]).exists()
    interrupted = body(old)
    record = manager._store.load(canceled["request_id"])
    view = {**record["view"], "job_id": interrupted["request_id"], "state": "running", "version": 4}
    request = record["request"]
    request["revision"]["version"] = 4
    from video_action_agent.notes_service import _signature
    from video_action_agent.task_skills import stored_skill

    request["signature"] = _signature(request["source_id"], request["source_digest"],
                                      request["user_request"], request["output_kind"],
                                      stored_skill(request["skill"], request["output_kind"]),
                                      request["revision"])
    manager._store.save(interrupted["request_id"], view, request)
    final_provider = RevisionProvider()
    restarted = NotesService("alice", manager.output_root, lambda: final_provider)
    assert restarted.get(interrupted["request_id"])["state"] == "interrupted"
    assert final_provider.calls == 0
    assert restarted.candidate(original) == old
    retry = body(old)
    restarted.revise(original, retry["request_id"], retry["expected_parent_fingerprint"],
                     retry["instruction"], allow_model_text=True)
    restarted.wait(retry["request_id"], 3)
    assert restarted.candidate(retry["request_id"])["revision"]["version"] == 5
    assert {row["job_id"]: row["version"] for row in restarted.history()["results"]} == {
        original: 1, failed["request_id"]: 2, canceled["request_id"]: 3,
        interrupted["request_id"]: 4, retry["request_id"]: 5}
    assert final_provider.calls == 1
