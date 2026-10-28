import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_source_candidate_bundle import candidate

from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_service import NotesService, NotesServiceError


class AnswerProvider:
    model_id = "controlled-answer-provider"

    def __init__(self):
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        body = json.loads(user)
        return json.dumps({"source_id": body["source_id"], "request_id": body["request_id"],
                           "source_status": "partial", "answer": [{"text": "材料提到的操作",
                           "segment_indices": [1]}], "missing": ["材料未给出完整代码"],
                           "unverified": []}, ensure_ascii=False)


def test_answer_api_save_restore_identity_and_reading(tmp_path):
    provider = AnswerProvider()
    bundle = candidate(tmp_path)
    output = tmp_path / "answers"
    manager = NotesService("alice", output, lambda: provider, saved_bundle=bundle)
    client = TestClient(create_ui_app(object(), {"a": "alice", "b": "bob"},
                                     source_modes=(), notes_service=manager),
                        base_url="http://127.0.0.1")
    body = {"request_id": str(uuid4()), "source_id": "saved", "user_request": "如何操作？",
            "allow_model_text": True, "output_kind": "source_answer"}
    headers = {"Authorization": "Bearer a"}
    assert client.post("/app/notes", json={**body, "allow_model_text": False},
                       headers=headers).status_code == 403
    assert client.post("/app/notes", json=body,
                       headers={"Authorization": "Bearer b"}).status_code == 404
    response = client.post("/app/notes", json=body, headers=headers)
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    manager.wait(job_id, 3)
    result = client.get(f"/app/notes/{job_id}/candidate", headers=headers)
    assert result.status_code == 200
    value = result.json()
    assert value["candidate"]["source_status"] == "partial"
    assert value["skill"]["id"] == "source-answer"
    assert value["completed"] is False and value["formal_task"] is False
    assert "如何操作？" in value["reading_markdown"]
    assert "材料未给出完整代码" in value["reading_markdown"]
    assert "controlled-answer-provider" not in value["reading_markdown"]
    assert "controlled-answer-provider" in value["markdown"]
    assert (output / job_id / "answer.md").is_file()
    restarted = NotesService("alice", output, lambda: provider, saved_bundle=bundle)
    assert restarted.candidate(job_id) == value
    restarted.start(job_id, "saved", body["user_request"], allow_model_text=True,
                    output_kind="source_answer")
    assert provider.calls == 1
    with pytest.raises(NotesServiceError, match="notes_request_conflict"):
        restarted.start(job_id, "saved", "更换问题", allow_model_text=True,
                        output_kind="source_answer")
    with pytest.raises(NotesServiceError, match="notes_candidate_unavailable"):
        NotesService("bob", output, lambda: None).candidate(job_id)
