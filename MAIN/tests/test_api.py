import asyncio

import httpx
import pytest
from sqlalchemy import text
from test_runtime import REQUEST


@pytest.fixture
def client(runtime):
    from video_action_agent.api import create_app

    app = create_app(runtime, {"alice-token": "alice", "bob-token": "bob"})

    class Client:
        def request(self, method, path, **kwargs):
            async def send():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    return await client.request(method, path, **kwargs)

            return asyncio.run(send())

        def post(self, path, **kwargs):
            return self.request("POST", path, **kwargs)

        def get(self, path, **kwargs):
            return self.request("GET", path, **kwargs)

    return Client()


def headers(key="one", token="alice-token"):
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


def test_api_admission_poll_cancel(client):
    r = client.post("/tasks", json=REQUEST, headers=headers())
    assert r.status_code == 202
    task = r.json()
    path = "/tasks/" + task["task_id"]
    assert client.get(path, headers=headers()).json()["completed"] is False
    r = client.post(path + "/cancel", json={"expected_revision": 1}, headers=headers())
    assert r.status_code == 200
    assert r.json()["contract_state"] == "canceled"


def test_api_auth_ownership_and_input_limits(client):
    assert client.post("/tasks", json=REQUEST).status_code == 401
    r = client.post("/tasks", json=REQUEST, headers=headers())
    path = "/tasks/" + r.json()["task_id"]
    assert client.get(path, headers=headers(token="bob-token")).status_code == 404
    assert (
        client.post(
            "/tasks", json={**REQUEST, "schema_version": 99}, headers=headers("x")
        ).status_code
        == 422
    )
    assert (
        client.post("/tasks", json={**REQUEST, "message": " "}, headers=headers("x")).status_code
        == 422
    )
    assert (
        client.post(
            "/tasks", json={**REQUEST, "send_email": True}, headers=headers("x")
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/tasks", json=REQUEST, headers={"Authorization": "Bearer alice-token"}
        ).status_code
        == 422
    )


def test_api_conflicts_and_revision(client):
    task = client.post("/tasks", json=REQUEST, headers=headers()).json()
    path = "/tasks/" + task["task_id"]
    assert (
        client.post("/tasks", json={**REQUEST, "message": "changed"}, headers=headers()).status_code
        == 409
    )
    assert (
        client.post(
            path + "/revisions",
            json={"expected_revision": 1, "request": {**REQUEST, "message": "Updated"}},
            headers=headers(),
        ).status_code
        == 200
    )
    assert (
        client.post(path + "/cancel", json={"expected_revision": 1}, headers=headers()).status_code
        == 409
    )


def test_local_browser_scope_is_explicit_versioned_input(client, engine):
    request = {**REQUEST, "local_browser_actions": ["video", "subtitle"]}
    created = client.post("/tasks", json=request, headers=headers("local-browser")).json()
    task_id = created["task_id"]
    with engine.connect() as conn:
        first = conn.execute(
            text("SELECT input FROM task_contracts WHERE task_id=:task AND version=1"),
            {"task": task_id},
        ).scalar_one()
        assert first["source"] == REQUEST["source"]
        assert first["local_browser_actions"] == ["video", "subtitle"]
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_reads WHERE task_id=:task"), {"task": task_id}
            ).scalar_one()
            == 0
        )

    revised = client.post(
        f"/tasks/{task_id}/revisions",
        json={"expected_revision": 1, "request": {**REQUEST, "message": "Updated"}},
        headers=headers(),
    )
    assert revised.status_code == 200
    with engine.connect() as conn:
        second = conn.execute(
            text("SELECT input FROM task_contracts WHERE task_id=:task AND version=2"),
            {"task": task_id},
        ).scalar_one()
        assert second.get("local_browser_actions", []) == []
        assert dict(
            conn.execute(
                text("SELECT owner_id,current_version FROM tasks WHERE task_id=:task"),
                {"task": task_id},
            )
            .one()
            ._mapping
        ) == {"owner_id": "alice", "current_version": 2}


@pytest.mark.parametrize(
    "source,actions",
    [
        (None, ["subtitle"]),
        ("https://example.com/video/BV1gD421u75J", ["video"]),
        (REQUEST["source"], ["video", "video"]),
        (REQUEST["source"], ["other"]),
    ],
)
def test_local_browser_scope_rejects_missing_or_invalid_target(client, source, actions):
    response = client.post(
        "/tasks",
        json={**REQUEST, "source": source, "local_browser_actions": actions},
        headers=headers("invalid-scope"),
    )
    assert response.status_code == 422
