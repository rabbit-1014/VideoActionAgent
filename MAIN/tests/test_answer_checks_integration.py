"""Answer-only checking, explicit authority and immutable revision boundaries."""

import json
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_source_answer_service import AnswerProvider
from test_source_candidate_bundle import candidate

from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_checks import NotesCheckError, NotesChecks
from video_action_agent.notes_service import NotesService

AUTH = {"Authorization": "Bearer a"}
QUESTION = "说明操作步骤与选择规则，不补完整代码"


class Checker:
    model_id = "controlled-answer-checker"

    def __init__(self, started=None, release=None):
        self.calls = []
        self.started, self.release = started, release

    def complete(self, system, user):
        body = json.loads(user)
        self.calls.append(body)
        if "candidate_text" not in body:
            if self.started:
                self.started.set()
                assert self.release.wait(4)
            return json.dumps({"request_fingerprint": body["request_fingerprint"],
                               "requirements": [{"kind": "content", "text": "说明选择规则",
                               "basis": [{"version": 0, "quote": body["user_request"]}]}]})
        result = {key: body[key] for key in ("request_fingerprint", "requirements_fingerprint",
                                            "candidate_fingerprint", "source_fingerprint")}
        result["findings"] = [{"requirement_id": "r001", "status": "unmet",
                               "explanation": "回答未说明选择规则。", "candidate_excerpt": "",
                               "source_indices": [1], "source_support": "supported"}]
        return json.dumps(result)


def setup_answer(tmp_path, checker, question=QUESTION, generator=None):
    generator = generator or AnswerProvider()
    notes = NotesService("alice", tmp_path / "answers", lambda: generator,
                         saved_bundle=candidate(tmp_path))
    identity = str(uuid4())
    notes.start(identity, "saved", question, output_kind="source_answer", allow_model_text=True)
    notes.wait(identity, 3)
    checks = NotesChecks(notes, lambda: checker)
    client = TestClient(create_ui_app(object(), {"a": "alice", "b": "bob"}, source_modes=(),
                                     notes_service=notes, notes_checks=checks),
                        base_url="http://127.0.0.1")
    return notes, checks, client, identity, generator


def test_answer_body_not_question_is_checked_and_cold_read_never_replays(tmp_path):
    checker = Checker()
    notes, checks, client, identity, generator = setup_answer(tmp_path, checker)
    check_id = str(uuid4())
    try:
        original = notes.candidate(identity)
        response = client.post(f"/app/notes/{identity}/checks", headers=AUTH,
                               json={"request_id": check_id, "allow_model_text": True})
        assert response.status_code == 200
        checks.wait(check_id, 3)
        result = checks.result(check_id)
        assert result["evaluation"]["outcome"] == "unmet"
        assert result["completed"] is result["quality_approved"] is False
        assert QUESTION not in checker.calls[1]["candidate_text"]
        assert "材料提到的操作" in checker.calls[1]["candidate_text"]
        assert "材料未给出完整代码" in checker.calls[1]["candidate_text"]
        assert "candidate_text" not in checker.calls[0] and "segments" not in checker.calls[0]
        assert notes.candidate(identity) == original
        checks.close()
        cold = NotesChecks(notes, lambda: checker)
        assert cold.result(check_id) == result
        assert cold.start(check_id, identity, allow_model_text=True)["state"] == "available"
        assert len(checker.calls) == 2 and generator.calls == 1
        cold.close()
    finally:
        checks.close()
        notes.close()


def test_answer_check_excerpts_preserve_quotes_and_real_line_breaks(tmp_path):
    excerpt = 'Select "key" after sorting\nThen output'

    class QuotedAnswer(AnswerProvider):
        def complete(self, system, user):
            reply = json.loads(super().complete(system, user))
            reply["answer"][0]["text"] = excerpt
            return json.dumps(reply)

    class QuotedChecker(Checker):
        def complete(self, system, user):
            reply = json.loads(super().complete(system, user))
            if "findings" in reply:
                reply["findings"][0].update(status="met", candidate_excerpt=excerpt)
            return json.dumps(reply)

    checker = QuotedChecker()
    notes, checks, _, identity, _ = setup_answer(tmp_path, checker, generator=QuotedAnswer())
    try:
        check_id = str(uuid4())
        checks.start(check_id, identity, allow_model_text=True)
        checks.wait(check_id, 3)
        assert checks.get(check_id)["state"] == "available"
        assert checks.result(check_id)["evaluation"]["findings"][0]["candidate_excerpt"] == excerpt
        assert excerpt in checker.calls[1]["candidate_text"]
        assert QUESTION not in checker.calls[1]["candidate_text"]
    finally:
        checks.close()
        notes.close()


def test_saved_legacy_answer_snapshot_reads_without_weakening_identity(tmp_path):
    checker = Checker()
    notes, checks, _, identity, _ = setup_answer(tmp_path, checker)
    original_context = notes.inspection_context

    def legacy_context(job_id):
        context = original_context(job_id)
        answer = notes.candidate(job_id)["candidate"]
        context["candidate_text"] = json.dumps({
            "answer": [item["text"] for item in answer["answer"]],
            "missing": answer["missing"], "unverified": answer["unverified"],
        }, ensure_ascii=False, sort_keys=True, allow_nan=False)
        return context

    check_id = str(uuid4())
    try:
        notes.inspection_context = legacy_context
        checks.start(check_id, identity, allow_model_text=True)
        checks.wait(check_id, 3)
        expected = checks.result(check_id)
        checks.close()
        notes.inspection_context = original_context
        cold = NotesChecks(notes, lambda: pytest.fail("cold read must not call model"))
        try:
            assert cold.result(check_id) == expected
            assert cold.start(check_id, identity, allow_model_text=True)["state"] == "available"
            assert len(checker.calls) == 2
            row = cold._store.load(check_id)
            row["request"]["snapshot"]["version"] += 1
            cold._store.save(check_id, row["view"], row["request"])
            altered = NotesChecks(notes, lambda: pytest.fail("no model"))
            try:
                with pytest.raises(NotesCheckError, match="candidate_changed"):
                    altered.result(check_id)
            finally:
                altered.close()
        finally:
            cold.close()
    finally:
        notes.inspection_context = original_context
        checks.close()
        notes.close()


def test_answer_check_permission_cancel_and_late_return_preserve_candidate(tmp_path):
    started, release = threading.Event(), threading.Event()
    checker = Checker(started, release)
    notes, checks, client, identity, _ = setup_answer(tmp_path, checker)
    check_id = str(uuid4())
    route = f"/app/notes/{identity}/checks"
    body = {"request_id": check_id, "allow_model_text": True}
    try:
        original = notes.candidate(identity)
        assert client.post(route, json=body).status_code == 401
        assert client.post(route, json=body,
                           headers={"Authorization": "Bearer b"}).status_code == 404
        assert client.post(route, json={**body, "allow_model_text": False},
                           headers=AUTH).status_code == 403
        assert checker.calls == []
        assert client.post(route, json=body, headers=AUTH).status_code == 200
        assert started.wait(3)
        assert client.post(f"/app/notes-checks/{check_id}/cancel", headers=AUTH).status_code == 200
        release.set()
        checks.wait(check_id, 3)
        assert checks.get(check_id)["state"] == "canceled"
        assert client.get(f"/app/notes-checks/{check_id}/result", headers=AUTH).status_code == 409
        assert len(checker.calls) == 1
        assert notes.candidate(identity) == original
    finally:
        release.set()
        checks.close()
        notes.close()


def test_answer_method_declaration_stays_generic_and_revision_has_own_opinion(tmp_path):
    question = "保留手动文本、识别字幕、文稿匹配三种方法，每种按“适用情况—步骤—检查点”组织"
    checker = Checker()
    notes, checks, _, identity, _ = setup_answer(tmp_path, checker, question)
    check_id = str(uuid4())
    try:
        checks.start(check_id, identity, allow_model_text=True)
        checks.wait(check_id, 3)
        original = notes.candidate(identity)
        row = checks._store.load(check_id)
        assert row["request"]["schema_version"] == 1
        assert row["request"]["snapshot"]["output_kind"] == "source_answer"
        assert "sections" not in row["request"]["snapshot"]
        revised = notes.revise(identity, uuid4(), original["revision"]["record_fingerprint"],
                               "补清选择规则", allow_model_text=True)
        notes.wait(revised["job_id"], 3)
        assert checks.result(check_id)["candidate_id"] == identity
        assert checks.result(check_id)["candidate_id"] != revised["job_id"]
        assert notes.candidate(identity) == original
        assert len(checker.calls) == 2
    finally:
        checks.close()
        notes.close()
