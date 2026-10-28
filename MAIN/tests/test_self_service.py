from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_notes_service import Provider
from test_source_answer_service import AnswerProvider
from test_source_candidate_bundle import api, candidate

from video_action_agent.content_intake import IntakeError
from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_service import NotesService
from video_action_agent.source_capture import SourceCapture

SOURCE = "https://www.bilibili.com/video/BV1AJen6SEVQ?p=42"


def test_owned_source_restores_but_canceled_or_unknown_work_never_replays(tmp_path):
    bundle = candidate(tmp_path)
    bundle["acquisition"] = "live_anonymous_audio"
    calls = []
    cancel = [False]

    def capture(source, output, **kwargs):
        calls.append(source)
        kwargs["on_stage"]("saving")
        if cancel[0]:
            manager.cancel(output.name)
        api().save_bundle(deepcopy(bundle), output)

    manager = SourceCapture("alice", tmp_path / "sources", capture_fn=capture)
    first = str(uuid4())
    manager.start(first, SOURCE, allow_local_audio_read=True)
    manager.wait(first, 3)
    assert manager.get(first)["state"] == "available"
    assert len(manager.history()["jobs"]) == 1
    cancel[0] = True
    second = str(uuid4())
    manager.start(second, SOURCE, allow_local_audio_read=True)
    manager.wait(second, 3)
    assert manager.get(second)["state"] == "canceled"
    restarted = SourceCapture("alice", tmp_path / "sources", capture_fn=capture)
    assert restarted.get(first)["state"] == "available"
    assert restarted.bundle(first) == bundle
    assert restarted.get(second)["state"] == "canceled"
    with pytest.raises(IntakeError, match="intake_not_ready"):
        restarted.candidate(second)
    restarted.start(second, SOURCE, allow_local_audio_read=True)
    assert len(calls) == 2
    assert SourceCapture("bob", tmp_path / "sources", capture_fn=capture).history()["jobs"] == []
    unknown = str(uuid4())
    restarted._store.save(unknown, {"job_id": unknown, "state": "running", "stage": "downloading",
                          "bvid": bundle["source_envelope"]["source_evidence"]["bvid"],
                          "page": 42, "error_code": None},
                          {"bvid": bundle["source_envelope"]["source_evidence"]["bvid"],
                           "page": 42, "allow_local_audio_read": True})
    assert restarted.get(unknown)["state"] == "interrupted"
    restarted.start(unknown, SOURCE, allow_local_audio_read=True)
    assert len(calls) == 2


def test_results_restore_and_reuse_frozen_master_without_bootstrap_ids(tmp_path):
    bundle = candidate(tmp_path)
    providers = [Provider()]
    output = tmp_path / "results"
    manager = NotesService("alice", output, lambda: providers[-1], saved_bundle=bundle)
    first = str(uuid4())
    manager.start(first, "saved", "整理笔记", allow_model_text=True)
    manager.wait(first, 3)
    second = str(uuid4())
    providers.append(AnswerProvider())
    manager.start(second, "artifact:" + first, "如何操作？", allow_model_text=True,
                  output_kind="source_answer")
    manager.wait(second, 3)
    expected = manager.candidate(second)
    providers.append(Provider(fail=True))
    failed = str(uuid4())
    manager.start(failed, "saved", "失败请求", allow_model_text=True)
    manager.wait(failed, 3)
    calls = sum(provider.calls for provider in providers)
    restarted = NotesService("alice", output, lambda: None)
    history = restarted.history()["results"]
    assert {row["job_id"] for row in history} == {first, second, failed}
    assert restarted.candidate(second) == expected
    assert restarted.get(failed)["state"] == "failed"
    sources = restarted.sources()["sources"]
    assert len(sources) == 1 and sources[0]["source_id"].startswith("artifact:")
    assert restarted._bundle(sources[0]["source_id"]) == bundle
    assert sum(provider.calls for provider in providers) == calls
    assert NotesService("bob", output, lambda: None).history()["results"] == []
    client = TestClient(create_ui_app(object(), {"a": "alice", "b": "bob"},
                                     source_modes=(), notes_service=restarted),
                        base_url="http://127.0.0.1")
    assert client.get("/app/notes/history").status_code == 401
    assert client.get("/app/notes/history", headers={"Authorization": "Bearer b"}).status_code == 404
    response = client.get("/app/notes/history", headers={"Authorization": "Bearer a"})
    assert response.status_code == 200
    assert "source_bundle" not in response.text and str(output) not in response.text
