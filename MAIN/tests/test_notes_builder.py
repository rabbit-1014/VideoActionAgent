import hashlib
import json
from copy import deepcopy

import pytest

from video_action_agent.notes_builder import (
    NotesBuildError,
    build_notes,
    render_notes,
    validate_notes,
)

SEGMENTS = [{"start": 1.0, "end": 3.0, "text": "video fact"}]
REF = {"kind": "audio_transcription", "bvid": "BV1AJen6SEVQ", "page": 42, "cid": 41961066345}
DIGEST = "a" * 64
REQUEST = "课程笔记"


class Provider:
    model_id = "controlled-provider"

    def __init__(self, mutate=lambda value: value):
        self.mutate = mutate

    def complete(self, system, user):
        body = json.loads(user)
        assert body["segments"][0]["text"] == SEGMENTS[0]["text"]
        result = {"source_id": body["source_id"], "request_id": body["request_id"],
                  "title": "<script>Title</script>", "sections": [{"title": "概念", "items": [
                  {"text": "[bad](javascript:alert(1)) <script>x</script>",
                   "segment_indices": [1]}]}], "unverified": []}
        return json.dumps(self.mutate(result))


def test_notes_precise_citations_safe_render_and_immutable_input():
    before = deepcopy(SEGMENTS)
    def cited_uncertainty(value):
        value["unverified"] = [{"text": "画面内容未取得", "segment_indices": [1]}]
        return value

    draft = build_notes(SEGMENTS, REQUEST, REF, DIGEST, Provider(cited_uncertainty))
    assert draft["request_id"] == hashlib.sha256(REQUEST.encode()).hexdigest()
    assert draft["sections"][0]["items"][0]["start"] == 1.0
    assert draft["sections"][0]["items"][0]["end"] == 3.0
    assert draft["status"] == "unreviewed_candidate" and draft["unverified"]
    assert validate_notes(draft, SEGMENTS, REQUEST, REF, DIGEST) == draft
    markdown = render_notes(draft)
    assert "<script>" not in markdown and "[bad](javascript:" not in markdown
    assert "p=42&t=1" in markdown and SEGMENTS == before
    draft["sections"][0]["items"][0]["end"] = 300.0
    with pytest.raises(NotesBuildError, match="notes_candidate_mismatch"):
        validate_notes(draft, SEGMENTS, REQUEST, REF, DIGEST)


def test_notes_invalid_identity_citation_and_empty_content_never_produce_candidate():
    def changed(value, field, content):
        value[field] = content
        return value

    changes = [lambda value: changed(value, "source_id", "b" * 64),
               lambda value: changed(value, "request_id", "wrong-request"),
               lambda value: changed(value, "sections", []),
               lambda value: changed(value, "sections", [{"title": "s", "items": [
                   {"text": "x", "segment_indices": [2]}]}]),
               lambda value: changed(value, "completed", True)]
    for mutate in changes:
        with pytest.raises(NotesBuildError):
            build_notes(SEGMENTS, REQUEST, REF, DIGEST, Provider(mutate))
