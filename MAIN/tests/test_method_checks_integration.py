"""Three bounded integration cases: one call, legacy cold read, failure fences."""

import json
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_notes_checks import AUTH, Checker
from test_source_candidate_bundle import candidate

from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_checks import NotesCheckError, NotesChecks
from video_action_agent.notes_service import NotesService

REQUEST = "保留重命名、复制两种方法，每种按“适用情况—步骤—检查点”组织。"


class Generator:
    model_id = "controlled-generator"

    def __init__(self):
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        body = json.loads(user)
        sections = []
        for name in ("重命名", "复制"):
            labels = ["适用情况", "步骤"] + (["检查点"] if name == "复制" else [])
            sections.append({"title": name, "items": [
                {"text": name + label + "的受控内容", "label": label, "segment_indices": [1]}
                for label in labels]})
        return json.dumps({"source_id": body["source_id"], "request_id": body["request_id"],
                           "title": "两种文件操作", "sections": sections,
                           "unverified": []}, ensure_ascii=False)


class MethodChecker:
    def __init__(self, started=None, release=None, bad=False):
        self.calls = []
        self.started, self.release, self.bad = started, release, bad

    def complete(self, system, user):
        body = json.loads(user)
        self.calls.append(body)
        if self.started:
            self.started.set()
            assert self.release.wait(4)
        if self.bad:
            return "unstructured private return"
        result = {key: body[key] for key in ("requirements_fingerprint",
                                            "candidate_fingerprint", "source_fingerprint")}
        result.update(bindings=[], cells=[], items=[])
        for method, section in zip(body["requirements"]["profile"]["methods"],
                                   body["sections"], strict=True):
            result["bindings"].append({"method_id": method["id"],
                                       "section_index": section["index"], "explanation": "受控绑定"})
            for field in body["requirements"]["profile"]["fields"]:
                result["cells"].append({"method_id": method["id"], "field": field["key"],
                    "item_indices": [item["index"] for item in section["items"]
                                     if item["label"] == field["label"]], "explanation": "按条目标签绑定"})
            for item in section["items"]:
                result["items"].append({"section_index": section["index"],
                    "item_index": item["index"], "clauses": [{"text": item["text"],
                    "role": "paraphrase", "source_quotes": []}]})
        return json.dumps(result, ensure_ascii=False)


def setup(tmp_path, checker):
    generator = Generator()
    notes = NotesService("alice", tmp_path / "notes", lambda: generator,
                         saved_bundle=candidate(tmp_path))
    identity = str(uuid4())
    notes.start(identity, "saved", REQUEST, allow_model_text=True)
    notes.wait(identity, 3)
    checks = NotesChecks(notes, lambda: checker)
    client = TestClient(create_ui_app(object(), {"a": "alice", "b": "bob"},
                        source_modes=(), notes_service=notes, notes_checks=checks),
                        base_url="http://127.0.0.1")
    return notes, checks, client, identity, generator


def test_declared_fields_one_call_saved_cold_read_and_same_request(tmp_path):
    checker = MethodChecker()
    notes, checks, client, identity, generator = setup(tmp_path, checker)
    request_id = str(uuid4())
    try:
        original = notes.candidate(identity)
        legacy_snapshot = notes.inspection_context(identity)
        assert "sections" not in legacy_snapshot
        expanded = notes.inspection_context(identity, include_sections=True)
        assert {key: value for key, value in expanded.items() if key != "sections"} == legacy_snapshot
        assert expanded["sections"] == original["candidate"]["sections"]
        body = {"request_id": request_id, "allow_model_text": True}
        route = f"/app/notes/{identity}/checks"
        assert client.post(route, json=body, headers=AUTH).status_code == 200
        checks.wait(request_id, 3)
        assert checks.get(request_id)["state"] == "available"
        result = client.get(f"/app/notes-checks/{request_id}/result", headers=AUTH).json()
        assert len(result["requirements"]["requirements"]) == 6
        missing = [item for item in result["evaluation"]["findings"] if item["coverage"] == "missing"]
        assert [(item["method_id"], item["field"]) for item in missing] == [("m001", "checkpoint")]
        assert result["evaluation"]["other_requirements_reviewed"] is False
        assert result["quality_approved"] is result["completed"] is False
        assert len(checker.calls) == 1 and generator.calls == 1
        persisted = checks._store.load(request_id)["request"]
        assert persisted["schema_version"] == 2
        assert persisted["requirements"] == result["requirements"]
        checks.close()
        cold = NotesChecks(notes, lambda: checker)
        assert cold.result(request_id) == result
        assert cold.start(request_id, identity, allow_model_text=True)["state"] == "available"
        assert len(checker.calls) == 1 and notes.candidate(identity) == original
        cold.close()
    finally:
        checks.close()
        notes.close()


def test_old_opinion_on_explicit_request_uses_old_schema_without_reinterpretation(tmp_path, monkeypatch):
    class LegacyChecker(Checker):
        def complete(self, system, user):
            raw = super().complete(system, user)
            if "candidate_text" not in json.loads(user):
                value = json.loads(raw)
                value["requirements"][0]["basis"][0]["quote"] = REQUEST
                return json.dumps(value)
            return raw
    checker = LegacyChecker()
    notes, checks, _, identity, _ = setup(tmp_path, checker)
    request_id = str(uuid4())
    try:
        # Simulate a historical V1 job even though today's request fits V2.
        import video_action_agent.notes_checks as module
        with monkeypatch.context() as old:
            old.setattr(module, "prepare_method_requirements", lambda *args: None)
            checks.start(request_id, identity, allow_model_text=True)
            checks.wait(request_id, 3)
        original = checks.result(request_id)
        assert checks._store.load(request_id)["request"]["schema_version"] == 1
        checks.close()
        cold = NotesChecks(notes, lambda: checker)
        assert cold.result(request_id) == original
        assert cold.start(request_id, identity, allow_model_text=True)["state"] == "available"
        assert len(checker.calls) == 2
        cold.close()
    finally:
        checks.close()
        notes.close()


def test_new_route_permission_cancel_failure_and_interrupted_no_replay(tmp_path):
    started, release = threading.Event(), threading.Event()
    checker = MethodChecker(started, release)
    notes, checks, client, identity, _ = setup(tmp_path, checker)
    request_id = str(uuid4())
    route = f"/app/notes/{identity}/checks"
    body = {"request_id": request_id, "allow_model_text": True}
    try:
        assert client.post(route, json=body).status_code == 401
        assert client.post(route, json=body, headers={"Authorization": "Bearer b"}).status_code == 404
        assert client.post(route, json={**body, "allow_model_text": False}, headers=AUTH).status_code == 403
        assert checker.calls == []
        assert client.post(route, json=body, headers=AUTH).status_code == 200
        assert started.wait(3)
        assert checks._store.load(request_id)["request"]["requirements"]["schema_version"] == 2
        checks.cancel(request_id)
        release.set()
        checks.wait(request_id, 3)
        assert checks.get(request_id)["state"] == "canceled" and len(checker.calls) == 1
        with pytest.raises(NotesCheckError, match="check_not_ready"):
            checks.result(request_id)
        checker.started = None
        checker.bad = True
        bad_id = str(uuid4())
        checks.start(bad_id, identity, allow_model_text=True)
        checks.wait(bad_id, 3)
        assert checks.get(bad_id)["state"] == "failed"
        assert checks.get(bad_id)["error_code"] == "invalid_review_reply"
        assert checks._store.load(bad_id)["request"]["requirements"] is not None
        row = checks._store.load(bad_id)
        row["view"].update(state="checking", error_code=None)
        checks._store.save(bad_id, row["view"], row["request"])
        checks.close()
        cold = NotesChecks(notes, lambda: checker)
        assert cold.start(bad_id, identity, allow_model_text=True)["state"] == "interrupted"
        assert len(checker.calls) == 2
        row = cold._store.load(bad_id)
        row["request"]["requirements"]["profile"]["methods"][0]["name"] = "篡改"
        cold._store.save(bad_id, row["view"], row["request"])
        cold.close()
        with pytest.raises(NotesCheckError, match="check_storage_unavailable"):
            NotesChecks(notes, lambda: checker).get(bad_id)
    finally:
        release.set()
        checks.close()
        notes.close()
