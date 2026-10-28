from fastapi.testclient import TestClient


def test_local_page_has_no_embedded_credentials_and_api_remains_authenticated(tmp_path, monkeypatch):
    from video_action_agent import local_ui_app

    (tmp_path / "index.html").write_text("<h1>本地视频助手</h1>", encoding="utf-8")
    monkeypatch.setattr(local_ui_app, "WEB_ROOT", tmp_path)
    active = {"running": False}
    app = local_ui_app.create_ui_app(object(), {"private-session": "alice"}, lambda: active,
                                    source_modes=("audio_transcription",))
    client = TestClient(app, base_url="http://127.0.0.1")
    page = client.get("/app")
    assert page.status_code == 200 and "private-session" not in page.text
    assert client.get("/app/status").json()["execution_enabled"] is False
    assert client.get("/app/status").json()["capabilities"] == ["method_list_audio_transcription"]
    active["running"] = True
    assert client.get("/app/status").json()["execution_enabled"] is True
    assert client.get("/tasks/8892c1ff-1ad0-4e3e-bd14-a687c47b9ff4").status_code == 401
    assert client.get("/app", headers={"host": "untrusted.example"}).status_code == 400


def test_ui_session_survives_reload_and_rejects_cross_origin_changes():
    from video_action_agent.local_ui_app import create_ui_app

    class StoredTask:
        def get(self, owner, task_id):
            return {"owner": owner, "task_id": str(task_id)}

    client = TestClient(create_ui_app(StoredTask(), {"fresh-session": "alice"}),
                        base_url="http://127.0.0.1")
    session = client.post("/app/session", headers={"Authorization": "Bearer fresh-session"})
    assert session.status_code == 200
    cookie = session.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/app/session").json()["connected"] is True
    assert client.get("/tasks/8892c1ff-1ad0-4e3e-bd14-a687c47b9ff4").json()["owner"] == "alice"
    assert client.post("/tasks", headers={"Origin": "https://untrusted.example"}, json={}).status_code == 403


def test_ui_worker_filters_audio_tasks_without_changing_default_worker(runtime):
    from video_action_agent.product_worker import ProductTaskWorker

    subtitle = runtime.submit("alice", "ui-subtitle", {
        "message": "整理方法", "source": "BV1gD421u75J", "output_kind": "method_list",
        "local_browser_actions": ["subtitle"],
    })
    audio = runtime.submit("alice", "ui-audio", {
        "message": "整理方法", "source": "BV1AJen6SEVQ", "output_kind": "method_list",
        "source_mode": "audio_transcription", "local_source_actions": ["audio_transcription"],
    })
    scoped = ProductTaskWorker(runtime, None, "alice", source_modes=("subtitle",))
    assert scoped.eligible_task_ids() == [subtitle["task_id"]]
    audio_scoped = ProductTaskWorker(runtime, None, "alice", source_modes=("audio_transcription",))
    assert audio_scoped.eligible_task_ids() == [audio["task_id"]]
    assert set(ProductTaskWorker(runtime, None, "alice").eligible_task_ids()) == {
        subtitle["task_id"], audio["task_id"],
    }
