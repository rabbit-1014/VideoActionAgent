import json
from copy import deepcopy

import pytest
from test_notes_builder import DIGEST, REF, REQUEST, SEGMENTS, Provider

from video_action_agent.notes_builder import NotesBuildError, build_notes, validate_notes
from video_action_agent.task_skills import render_reading


class LayoutProvider(Provider):
    def complete(self, system, user):
        value = json.loads(super().complete(system, user))
        value["title"] = "分块与实体抽取"
        value["sections"][0]["title"] = "工作机制"
        value["sections"][0]["items"] = [
            {"label": "分块", "layout": "bullet", "text": "第一项事实", "segment_indices": [1]},
            {"label": "<script>抽取</script>", "layout": "bullet", "text": "第二项事实",
             "segment_indices": [1]},
            {"label": "检查", "layout": "step", "text": "先检查输入", "segment_indices": [1]},
            {"layout": "step", "text": "再处理输入", "segment_indices": [1]}]
        return "```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"


def test_real_structure_drives_toc_labels_lists_without_source_guessing():
    draft = build_notes(SEGMENTS, REQUEST, REF, DIGEST, LayoutProvider())
    assert draft["prompt_version"] == "notes-v2"
    assert validate_notes(draft, SEGMENTS, REQUEST, REF, DIGEST) == draft
    markdown = render_reading(draft)
    assert "## 目录" in markdown and "(#note-topic-1)" in markdown
    assert 'id="note-topic-1"' in markdown and "## 1. 工作机制" in markdown
    assert "- **分块**：第一项事实" in markdown
    assert "1. **检查**：先检查输入" in markdown and "2. 再处理输入" in markdown
    assert "<script>" not in markdown and "使用前需要注意" in markdown
    assert "?p=" not in markdown and draft["sections"][0]["items"][0]["start"] == 1.0
    class ExtraText(LayoutProvider):
        def complete(self, system, user):
            return super().complete(system, user) + "\nextra commentary"

    with pytest.raises(NotesBuildError, match="invalid_notes_reply"):
        build_notes(SEGMENTS, REQUEST, REF, DIGEST, ExtraText())


def test_legacy_layout_and_rejected_new_fields_keep_validation_strict():
    old = build_notes(SEGMENTS, REQUEST, REF, DIGEST, Provider())
    old["prompt_version"] = "notes-v1"
    assert validate_notes(old, SEGMENTS, REQUEST, REF, DIGEST) == old
    changed = deepcopy(old)
    changed["sections"][0]["items"][0]["layout"] = "bullet"
    with pytest.raises(NotesBuildError):
        validate_notes(changed, SEGMENTS, REQUEST, REF, DIGEST)
    draft = build_notes(SEGMENTS, REQUEST, REF, DIGEST, LayoutProvider())
    for key, value in (("layout", "html"), ("label", " "), ("segment_indices", [99]),
                       ("end", 999.0)):
        bad = deepcopy(draft)
        bad["sections"][0]["items"][0][key] = value
        with pytest.raises(NotesBuildError):
            validate_notes(bad, SEGMENTS, REQUEST, REF, DIGEST)
