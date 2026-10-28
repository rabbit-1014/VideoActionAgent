import hashlib
import json
from uuid import uuid4

import pytest
from test_master_methods import MethodProvider
from test_source_candidate_bundle import candidate

from video_action_agent.notes_service import NotesService, _signature
from video_action_agent.task_skills import SkillProvider, load_skill, render_reading


def test_fixed_selection_and_bounded_prompt():
    class Capture:
        model_id = "capture"

        def complete(self, system, user):
            self.received = system, user
            return "unchanged reply"

    provider = Capture()
    notes = load_skill("structured_notes")
    methods = load_skill("method_list")
    assert notes.skill_id != methods.skill_id
    wrapped = SkillProvider(provider, notes)
    assert wrapped.complete("strict JSON contract", "exact source") == "unchanged reply"
    assert provider.received[1] == "exact source"
    assert "strict JSON contract" in provider.received[0]
    assert notes.instructions in provider.received[0]
    assert wrapped.model_id == "capture"
    with pytest.raises(ValueError):
        load_skill("unknown")


def test_saved_skill_reading_restart_and_legacy_remain_honest(tmp_path, monkeypatch):
    provider = MethodProvider()
    bundle = candidate(tmp_path)
    root = tmp_path / "results"
    manager = NotesService("alice", root, lambda: provider, saved_bundle=bundle)
    identity = str(uuid4())
    manager.start(identity, "saved", "操作材料", allow_model_text=True, output_kind="method_list")
    manager.wait(identity, 3)
    result = manager.candidate(identity)
    assert result["skill"]["id"] == "operational-material"
    reading = result["reading_markdown"]
    assert "按原片段的方法处理" in reading and "现行" in reading
    assert "原片段" not in reading.replace("按原片段的方法处理", "")
    assert "BV1" not in reading and "?p=" not in reading
    assert "controlled-method-provider" not in reading
    assert "BV1" in result["markdown"]
    restarted = NotesService("alice", root, lambda: None, saved_bundle=bundle)
    def unavailable(_):
        raise OSError("Today's skill is unavailable")

    monkeypatch.setattr("video_action_agent.notes_service.load_skill", unavailable)
    assert restarted.candidate(identity) == result
    restarted.start(identity, "saved", "操作材料", allow_model_text=True, output_kind="method_list")
    assert provider.calls == 1

    # An old package remains readable without claiming that a Skill generated it.
    path = root / identity / "candidate.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record.pop("skill")
    record["signature"] = _signature("saved", result["candidate"]["payload_hash"],
                                      "操作材料", "method_list")
    path.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    manifest_path = root / identity / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["candidate.json"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    legacy = NotesService("alice", root, lambda: None).candidate(identity)
    assert legacy["skill"] is None and legacy["reading_markdown"] == reading
    unsafe = {"kind": "structured_notes", "title": "<script>x</script>",
              "sections": [], "terms": [], "examples": [], "unverified": []}
    assert "<script>" not in render_reading(unsafe)
