from copy import deepcopy
from uuid import uuid4

from fastapi.testclient import TestClient

from video_action_agent.product_worker import ProductTaskWorker
from video_action_agent.runtime import NotFound


def test_revised_task_does_not_wait_for_previous_version_cooldown():
    task_id = uuid4()
    view = {"task_id": task_id, "current_version": 1, "revision": 1,
            "contract_state": "accepted", "completed": False}
    calls, observations = [], []

    class Tasks:
        def get(self, owner, identity):
            assert owner == "alice" and identity == task_id
            return deepcopy(view)

    class Pipeline:
        def run_until_idle(self, identity):
            calls.append(identity)
            return {"stage": "source", "state": "waiting_for_browser", "code": "bridge_unavailable"}

    worker = ProductTaskWorker(Tasks(), Pipeline(), "alice")
    worker.eligible_task_ids = lambda limit=10: [task_id]
    worker.run_cycle()
    worker.run_cycle()
    assert len(calls) == 1
    view.update(current_version=2, revision=2)
    worker.run_cycle()
    assert len(calls) == 2
    view.update(current_version=3, revision=3)
    worker.run_cycle(on_result=observations.append)
    assert len(observations) == 1
    assert observations[0]["contract_version"] == 3 and observations[0]["retry_at"]
    view.update(current_version=4, revision=4)

    def revised_during_processing(identity):
        view.update(current_version=5, revision=5)
        return {"stage": "artifact", "state": "retryable_failure"}

    worker.pipeline.run_until_idle = revised_during_processing
    worker.run_cycle(on_result=observations.append)
    assert len(observations) == 1


def test_processing_hint_requires_current_owner_and_scope_and_never_echoes_payload():
    from video_action_agent.local_ui_app import create_ui_app
    from video_action_agent.processing_observations import ProcessingObservations

    task_id = uuid4()
    view = {"task_id": task_id, "current_version": 1, "revision": 8,
            "contract_state": "accepted", "completed": False}

    class Tasks:
        def get(self, owner, identity):
            if owner != "alice" or identity != task_id:
                raise NotFound()
            return deepcopy(view)

    hints = ProcessingObservations()
    hints.record({"owner": "alice", "task_id": str(task_id), "contract_version": 1,
                  "revision": 8, "retry_at": "2026-09-27T10:00:00+00:00",
                  "result": {"stage": "artifact", "state": "retryable_failure",
                             "code": "DO-NOT-EXPOSE-KEY", "raw_payload": "PRIVATE"}})
    client = TestClient(create_ui_app(Tasks(), {"alice-token": "alice", "bob-token": "bob"},
                                     processing_status=hints.read), base_url="http://127.0.0.1")
    path = f"/app/tasks/{task_id}/processing"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer bob-token"}).status_code == 404
    client.post("/app/session", headers={"Authorization": "Bearer alice-token"})
    response = client.get(path)
    assert response.status_code == 200
    assert response.json()["observation"]["state"] == "retryable_failure"
    assert "DO-NOT-EXPOSE" not in response.text and "PRIVATE" not in response.text
    view.update(current_version=2, revision=9)
    assert client.get(path).json() == {"observation": None}
    view.update(current_version=1, revision=9)
    assert client.get(path).json() == {"observation": None}
    view.update(revision=8, completed=True)
    assert client.get(path).json() == {"observation": None}
