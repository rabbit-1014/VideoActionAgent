"""Decisive checks for independent requirements, immutable target and no replay."""

import json
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_notes_service import Provider
from test_source_candidate_bundle import candidate

from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_checks import NotesCheckError, NotesChecks
from video_action_agent.notes_service import NotesService

AUTH = {"Authorization": "Bearer a"}
REQUEST = "每种方法都给检查点，不补材料没有的细节"


class Checker:
    model_id = "controlled-strong-checker"

    def __init__(self, started=None, release=None, bad=False):
        self.calls = []
        self.started, self.release, self.bad = started, release, bad

    def complete(self, system, user):
        body = json.loads(user)
        self.calls.append(body)
        if "candidate_text" not in body:
            if self.started:
                self.started.set()
                assert self.release.wait(4)
            return json.dumps({"request_fingerprint": body["request_fingerprint"],
                               "requirements": [{"kind": "content", "text": "每种方法有检查点",
                               "basis": [{"version": 0, "quote": "每种方法都给检查点"}]}]})
        if self.bad:
            return "private unstructured reply"
        result = {key: body[key] for key in ("request_fingerprint", "requirements_fingerprint",
                                            "candidate_fingerprint", "source_fingerprint")}
        result["findings"] = [{"requirement_id": "r001", "status": "unmet",
                               "explanation": "该稿缺少请求的检查点，来源也不足以补写。",
                               "candidate_excerpt": "", "source_indices": [1],
                               "source_support": "missing"}]
        return json.dumps(result)


def setup(tmp_path, checker):
    generator = Provider()
    notes = NotesService("alice", tmp_path / "notes", lambda: generator,
                         saved_bundle=candidate(tmp_path))
    identity = str(uuid4())
    notes.start(identity, "saved", REQUEST, allow_model_text=True)
    notes.wait(identity, 3)
    checks = NotesChecks(notes, lambda: checker)
    app = create_ui_app(object(), {"a": "alice", "b": "bob"}, source_modes=(),
                        notes_service=notes, notes_checks=checks)
    return notes, checks, TestClient(app, base_url="http://127.0.0.1"), identity, generator


def test_original_requirements_precede_content_and_opinion_survives_restart(tmp_path):
    checker = Checker()
    notes, checks, client, identity, generator = setup(tmp_path, checker)
    try:
        originals = notes.candidate(identity)
        for instruction in ("保留检查点，按方法组织", "简洁一些，仍保留检查点"):
            current = notes.candidate(identity)
            new = notes.revise(identity, uuid4(), current["revision"]["record_fingerprint"],
                               instruction, allow_model_text=True)
            identity = new["job_id"]
            notes.wait(identity, 3)
        target = notes.candidate(identity)
        request_id = str(uuid4())
        body = {"request_id": request_id, "allow_model_text": True}
        route = f"/app/notes/{identity}/checks"
        assert client.post(route, json=body, headers=AUTH).status_code == 200
        checks.wait(request_id, 3)
        result = client.get(f"/app/notes-checks/{request_id}/result", headers=AUTH).json()
        assert result["evaluation"]["outcome"] == "unmet"
        assert result["completed"] is result["quality_approved"] is False
        assert result["requirements"]["user_request"] == REQUEST
        assert [item["version"] for item in result["requirements"]["amendments"]] == [2, 3]
        assert "candidate_text" not in checker.calls[0] and "segments" not in checker.calls[0]
        assert checker.calls[1]["requirements"] == result["requirements"]
        assert notes.candidate(identity) == target and generator.calls == 3
        assert notes.candidate(originals["revision"]["root_job_id"]) == originals
        assert client.post(route, json=body, headers=AUTH).json()["state"] == "available"
        checks.close()
        cold = NotesChecks(notes, lambda: checker)
        assert cold.result(request_id) == result
        assert cold.start(request_id, identity, allow_model_text=True)["state"] == "available"
        assert len(checker.calls) == 2
        cold.close()
    finally:
        checks.close()
        notes.close()


def test_permission_cancel_and_bad_model_never_publish_or_follow_up(tmp_path):
    started, release = threading.Event(), threading.Event()
    checker = Checker(started, release)
    notes, checks, client, identity, _ = setup(tmp_path, checker)
    route = f"/app/notes/{identity}/checks"
    request_id = str(uuid4())
    body = {"request_id": request_id, "allow_model_text": True}
    try:
        assert client.post(route, json=body).status_code == 401
        assert client.post(route, json=body, headers={"Authorization": "Bearer b"}).status_code == 404
        assert client.post(route, json={**body, "allow_model_text": False}, headers=AUTH).status_code == 403
        assert client.post(route, json={**body, "allow_model_text": "true"}, headers=AUTH).status_code == 422
        assert checker.calls == []
        assert client.post(route, json=body, headers=AUTH).status_code == 200
        assert started.wait(3)
        assert client.post(route, json={**body, "request_id": str(uuid4())},
                           headers=AUTH).status_code == 409
        assert client.post(f"/app/notes-checks/{request_id}/cancel", headers=AUTH).status_code == 200
        release.set()
        checks.wait(request_id, 3)
        assert checks.get(request_id)["state"] == "canceled" and len(checker.calls) == 1
        assert client.get(f"/app/notes-checks/{request_id}/result", headers=AUTH).status_code == 409
        assert client.post(route, json=body, headers=AUTH).json()["state"] == "canceled"
        checker.started = None
        checker.bad = True
        bad_id = str(uuid4())
        checks.start(bad_id, identity, allow_model_text=True)
        checks.wait(bad_id, 3)
        assert checks.get(bad_id)["state"] == "failed"
        assert checks._store.load(bad_id)["request"]["requirements"] is not None
        assert checks.get(bad_id)["result_available"] is False
        assert "private" not in json.dumps(checks.get(bad_id))
        assert len(checker.calls) == 3
    finally:
        release.set()
        checks.close()
        notes.close()


def test_unknown_restart_and_target_changes_do_not_replay(tmp_path):
    checker = Checker()
    notes, checks, _, identity, _ = setup(tmp_path, checker)
    check_id = str(uuid4())
    try:
        checks.start(check_id, identity, allow_model_text=True)
        checks.wait(check_id, 3)
        row = checks._store.load(check_id)
        row["view"].update(state="checking", result_available=False)
        row["request"]["evaluation"] = None
        row["request"]["result_digest"] = None
        checks._store.save(check_id, row["view"], row["request"])
        checks.close()
        cold = NotesChecks(notes, lambda: checker)
        assert cold.start(check_id, identity, allow_model_text=True)["state"] == "interrupted"
        assert len(checker.calls) == 2
        other_id = str(uuid4())
        notes.start(other_id, "saved", REQUEST, allow_model_text=True)
        notes.wait(other_id, 3)
        with pytest.raises(NotesCheckError, match="check_request_conflict"):
            cold.start(check_id, other_id, allow_model_text=True)
        corrupt = cold._store.root / ".jobs" / f"{uuid4()}.json"
        corrupt.write_text("{", encoding="utf-8")
        with pytest.raises(ValueError, match="job_storage_unavailable"):
            cold.start(uuid4(), identity, allow_model_text=True)
        assert len(checker.calls) == 2
        cold.close()
        # A successful opinion on another target cannot survive changed candidate bytes.
        finished = NotesChecks(notes, lambda: checker)
        good_id = str(uuid4())
        corrupt.unlink()
        finished.start(good_id, identity, allow_model_text=True)
        finished.wait(good_id, 3)
        (tmp_path / "notes" / identity / "notes.md").write_text("tampered", encoding="utf-8")
        with pytest.raises(ValueError, match="notes_candidate_unavailable"):
            finished.result(good_id)
        assert len(checker.calls) == 4
        finished.close()
    finally:
        checks.close()
        notes.close()
