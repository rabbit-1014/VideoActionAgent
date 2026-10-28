"""One submitted intent, two separately permitted existing child services."""

import threading
import time
from uuid import uuid4

from fastapi.testclient import TestClient
from test_notes_service import Provider
from test_source_candidate_bundle import api, candidate

from video_action_agent.content_intake import IntakeError
from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_service import NotesService
from video_action_agent.source_capture import SourceCapture

SOURCE = "https://www.bilibili.com/video/BV1AJen6SEVQ/?p=42"
AUTH = {"Authorization": "Bearer a"}


def setup(tmp_path, *, release=None, fail=False, provider=None):
    calls = []
    bundle = candidate(tmp_path)
    bundle["acquisition"] = "live_anonymous_audio"

    def acquire(source, output, **kwargs):
        calls.append(source)
        if release is not None:
            assert release.wait(4)
        if fail:
            raise IntakeError("source_unavailable")
        api().save_bundle(bundle, output)

    sources = SourceCapture("alice", tmp_path / "sources", capture_fn=acquire)
    provider = provider or Provider()
    notes = NotesService("alice", tmp_path / "notes", lambda: provider, source_capture=sources)
    app = create_ui_app(object(), {"a": "alice", "b": "bob"}, source_modes=(),
                        source_capture=sources, notes_service=notes)
    client = TestClient(app, base_url="http://127.0.0.1")
    body = {"request_id": str(uuid4()), "source": SOURCE,
            "user_request": "整理这节课的概念与步骤", "output_kind": "structured_notes",
            "allow_local_audio_read": True, "allow_model_text": True}
    return client, app, sources, notes, provider, calls, body


def settled(client, identity):
    for _ in range(100):
        value = client.get(f"/app/flows/{identity}", headers=AUTH).json()
        if value.get("state") in {"available", "failed", "canceled", "interrupted"}:
            return value
        time.sleep(0.03)
    raise AssertionError("flow did not settle")


def close(app, sources, notes):
    if hasattr(app.state, "video_flows"):
        app.state.video_flows.close()
    sources.close()
    notes.close()


def test_one_intent_links_exact_children_and_restart_only_reads(tmp_path):
    client, app, sources, notes, provider, calls, body = setup(tmp_path)
    try:
        response = client.post("/app/flows", json=body, headers=AUTH)
        assert response.status_code == 200
        value = settled(client, body["request_id"])
        assert value["state"] == "available"
        assert value["formal_task"] is False and value["completed"] is False
        assert value["result_available"] is True
        saved = notes.candidate(value["notes_job_id"])
        assert saved["candidate"]["user_request"] == body["user_request"]
        assert saved["candidate"]["source_ref"]["page"] == 42
        repeat = client.post("/app/flows", json=body, headers=AUTH).json()
        assert repeat["source_job_id"] == value["source_job_id"]
        assert repeat["notes_job_id"] == value["notes_job_id"]
        assert len(calls) == provider.calls == 1
        assert client.post("/app/flows", json={**body, "user_request": "另一个目标"},
                           headers=AUTH).status_code == 409
        assert client.get("/app/flows/history", headers=AUTH).json()["flows"][0]["flow_id"] == body["request_id"]
    finally:
        close(app, sources, notes)
    restored_sources = SourceCapture("alice", tmp_path / "sources",
                                     capture_fn=lambda *a, **k: calls.append("unexpected"))
    restored_notes = NotesService("alice", tmp_path / "notes", lambda: provider,
                                 source_capture=restored_sources)
    restored_app = create_ui_app(object(), {"a": "alice"}, source_modes=(),
                                source_capture=restored_sources, notes_service=restored_notes)
    restored = TestClient(restored_app, base_url="http://127.0.0.1")
    try:
        assert settled(restored, body["request_id"])["state"] == "available"
        assert restored.post("/app/flows", json=body, headers=AUTH).status_code == 200
        assert len(calls) == provider.calls == 1
    finally:
        close(restored_app, restored_sources, restored_notes)
    # A missing source journal must not let another video's saved child impersonate this intent.
    (tmp_path / "sources" / ".jobs" / f"{value['source_job_id']}.json").write_text("{", encoding="utf-8")
    (tmp_path / "notes" / ".flows" / ".jobs" / f"{body['request_id']}.json").unlink()
    isolated_sources = SourceCapture("alice", tmp_path / "sources",
                                      capture_fn=lambda *a, **k: calls.append("unexpected"))
    isolated_notes = NotesService("alice", tmp_path / "notes", lambda: provider,
                                 source_capture=isolated_sources)
    isolated_app = create_ui_app(object(), {"a": "alice"}, source_modes=(),
                                source_capture=isolated_sources, notes_service=isolated_notes)
    try:
        isolated = TestClient(isolated_app, base_url="http://127.0.0.1")
        other = {**body, "source": "https://www.bilibili.com/video/BV1gD421u75J/?p=1"}
        assert isolated.post("/app/flows", json=other, headers=AUTH).status_code == 200
        rejected = settled(isolated, body["request_id"])
        assert rejected["state"] == "failed" and rejected["result_available"] is False
        assert rejected["error_code"] == "flow_child_mismatch"
        assert len(calls) == provider.calls == 1
    finally:
        close(isolated_app, isolated_sources, isolated_notes)


def test_dual_permission_owner_and_cancel_prevent_followup(tmp_path):
    release = threading.Event()
    client, app, sources, notes, provider, calls, body = setup(tmp_path, release=release)
    try:
        for permission in ("allow_local_audio_read", "allow_model_text"):
            assert client.post("/app/flows", json={**body, permission: False},
                               headers=AUTH).status_code == 403
        assert client.post("/app/flows", json=body).status_code == 401
        assert client.post("/app/flows", json=body,
                           headers={"Authorization": "Bearer b"}).status_code == 404
        assert client.post("/app/flows", json={**body, "output_kind": "other"},
                           headers=AUTH).status_code == 422
        assert calls == [] and provider.calls == 0
        assert client.post("/app/flows", json=body, headers=AUTH).status_code == 200
        for _ in range(100):
            if calls:
                break
            time.sleep(0.01)
        assert len(calls) == 1
        stopped = client.post(f"/app/flows/{body['request_id']}/cancel", headers=AUTH)
        assert stopped.status_code == 200
        release.set()
        assert settled(client, body["request_id"])["state"] == "canceled"
        time.sleep(0.1)
        assert provider.calls == 0
        assert client.post("/app/flows", json=body, headers=AUTH).json()["state"] == "canceled"
        assert provider.calls == 0 and len(calls) == 1
    finally:
        release.set()
        close(app, sources, notes)


def test_failures_and_interrupted_journal_never_replay(tmp_path):
    client, app, sources, notes, provider, calls, body = setup(tmp_path, fail=True)
    try:
        assert client.post("/app/flows", json=body, headers=AUTH).status_code == 200
        value = settled(client, body["request_id"])
        assert value["state"] == "failed" and value["error_code"] == "source_unavailable"
        assert value["source_state"] == "failed" and value["notes_state"] is None
        assert len(calls) == 1 and provider.calls == 0
        assert client.post("/app/flows", json=body, headers=AUTH).json()["state"] == "failed"
        assert len(calls) == 1 and provider.calls == 0
        manager = app.state.video_flows
        record = manager._store.load(body["request_id"])
        record["view"].update(state="building", error_code=None)
        unknown_id = str(uuid4())
        record["view"]["flow_id"] = record["view"]["job_id"] = unknown_id
        record["request"]["request_id"] = unknown_id
        manager._store.save(unknown_id, record["view"], record["request"])
    finally:
        close(app, sources, notes)
    # This forged root cannot adopt another flow's child jobs or execute new work.
    fresh_sources = SourceCapture("alice", tmp_path / "sources",
                                  capture_fn=lambda *a, **k: calls.append("unexpected"))
    fresh_notes = NotesService("alice", tmp_path / "notes", lambda: provider,
                              source_capture=fresh_sources)
    fresh_app = create_ui_app(object(), {"a": "alice"}, source_modes=(),
                             source_capture=fresh_sources, notes_service=fresh_notes)
    try:
        fresh = TestClient(fresh_app, base_url="http://127.0.0.1")
        response = fresh.get(f"/app/flows/{unknown_id}", headers=AUTH)
        assert response.status_code == 404
        assert len(calls) == 1 and provider.calls == 0
    finally:
        close(fresh_app, fresh_sources, fresh_notes)
    interrupted_root = tmp_path / "interrupted"
    interrupted_root.mkdir()
    release = threading.Event()
    active, active_app, active_sources, active_notes, active_provider, active_calls, pending = setup(
        interrupted_root, release=release)
    assert active.post("/app/flows", json=pending, headers=AUTH).status_code == 200
    for _ in range(100):
        if active_calls:
            break
        time.sleep(0.01)
    close(active_app, active_sources, active_notes)
    release.set()
    reopened_sources = SourceCapture("alice", interrupted_root / "sources",
                                     capture_fn=lambda *a, **k: active_calls.append("unexpected"))
    reopened_notes = NotesService("alice", interrupted_root / "notes", lambda: active_provider,
                                 source_capture=reopened_sources)
    reopened_app = create_ui_app(object(), {"a": "alice"}, source_modes=(),
                                source_capture=reopened_sources, notes_service=reopened_notes)
    try:
        reopened = TestClient(reopened_app, base_url="http://127.0.0.1")
        assert settled(reopened, pending["request_id"])["state"] == "interrupted"
        assert reopened.post("/app/flows", json=pending, headers=AUTH).json()["state"] == "interrupted"
        assert len(active_calls) == 1 and active_provider.calls == 0
    finally:
        close(reopened_app, reopened_sources, reopened_notes)
