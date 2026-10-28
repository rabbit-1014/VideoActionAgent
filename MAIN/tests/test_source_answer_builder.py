import hashlib
import json
from copy import deepcopy

import pytest

from video_action_agent.notes_builder import ASR_LIMIT, NotesBuildError
from video_action_agent.source_answer_builder import (
    build_source_answer,
    render_source_answer,
    validate_source_answer,
)

SEGMENTS = [{"start": 1.0, "end": 3.0, "text": "video fact one"},
            {"start": 5.0, "end": 7.5, "text": "video fact two"}]
REF = {"kind": "audio_transcription", "bvid": "BV1AJen6SEVQ", "page": 42, "cid": 41961066345}
DIGEST = "a" * 64
REQUEST = "这条视频讲了什么？"


class Provider:
    model_id = "controlled-provider"

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        body = json.loads(user)
        base = {"source_id": body["source_id"], "request_id": body["request_id"],
                "source_status": "answered",
                "answer": [{"text": "<b>视频</b>[bad](javascript:alert(1)) 讲了一个事实",
                            "segment_indices": [2, 1, 1]}],
                "missing": [], "unverified": []}
        return f"```json\n{json.dumps({**base, **self.reply}, ensure_ascii=False)}\n```"


def test_answer_statuses_strictly_validate_and_render_safely():
    cases = [
        ("answered", {}, ("## 回答",)),
        ("partial", {"source_status": "partial", "missing": ["缺少画面演示"]},
         ("## 回答", "## 缺少材料")),
        ("not_in_source", {"source_status": "not_in_source", "answer": [],
                           "missing": ["片段未涉及该问题"]}, ("## 缺少材料",)),
    ]
    for status, reply, headings in cases:
        before = deepcopy(SEGMENTS)
        provider = Provider(reply)
        draft = build_source_answer(SEGMENTS, REQUEST, REF, DIGEST, provider)
        assert provider.calls == 1 and SEGMENTS == before
        assert draft["schema_version"] == 1 and draft["kind"] == "source_answer"
        assert draft["status"] == "unreviewed_candidate" and draft["prompt_version"] == "answer-v1"
        assert draft["request_id"] == hashlib.sha256(REQUEST.encode()).hexdigest()
        assert draft["source_status"] == status and draft["source_ref"] == REF
        for item in draft["answer"]:
            assert item["segment_indices"] == [1, 2]
            assert item["start"] == 1.0 and item["end"] == 7.5
        assert ASR_LIMIT in draft["unverified"]
        assert validate_source_answer(draft, SEGMENTS, REQUEST, REF, DIGEST) == draft
        markdown = render_source_answer(draft)
        assert REQUEST in markdown and "BV1AJen6SEVQ" in markdown
        assert "controlled-provider" in markdown
        assert ("p=42&t=1" in markdown) == bool(draft["answer"])
        assert "<b>" not in markdown and "[bad](javascript:" not in markdown
        assert all(heading in markdown for heading in headings)


def test_invalid_replies_and_tampered_candidates_never_validate():
    builds = [
        ({"source_id": "b" * 64}, "answer_identity_mismatch"),
        ({"request_id": "wrong"}, "answer_identity_mismatch"),
        ({"source_status": "not_in_source", "answer": []}, "invalid_answer_reply"),
        ({"source_status": "partial"}, "invalid_answer_reply"),
        ({"source_status": "answered", "missing": ["缺口"]}, "invalid_answer_reply"),
        ({"answer": [{"text": "x", "segment_indices": [3]}]}, "invalid_citation"),
        ({"answer": ["正文中夹带 segments 1, 2"]}, "invalid_answer_reply"),
        ({"missing": ["   "]}, "invalid_answer_reply"),
        ({"unverified": [123]}, "invalid_answer_reply"),
        ({"extra": "field"}, "invalid_answer_reply"),
    ]
    for reply, code in builds:
        with pytest.raises(NotesBuildError, match=code):
            build_source_answer(SEGMENTS, REQUEST, REF, DIGEST, Provider(reply))
    draft = build_source_answer(SEGMENTS, REQUEST, REF, DIGEST, Provider({}))
    changes = [lambda value: value["answer"][0].update(end=300.0),
               lambda value: value.update(schema_version=True),
               lambda value: value["answer"][0].update(start=True),
               lambda value: value.update(source_status="partial"),
               lambda value: value["answer"][0].update(segment_indices=[3]),
               lambda value: value.update(completed=True),
               lambda value: value["missing"].append("  ")]
    for change in changes:
        tampered = deepcopy(draft)
        change(tampered)
        with pytest.raises(NotesBuildError, match="answer_candidate_mismatch"):
            validate_source_answer(tampered, SEGMENTS, REQUEST, REF, DIGEST)
