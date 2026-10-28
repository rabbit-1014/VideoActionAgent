import copy
import json

import pytest

from video_action_agent import artifact_builder as builder
from video_action_agent import artifact_quality as quality

SEGMENTS = [{"start": 1.0, "end": 3.0, "text": "打开设置并检查开关。"}]
REQUEST = "整理方法"
HASH = "a" * 64


def source_ref():
    return {
        "bvid": "BV1AJen6SEVQ", "page": 42, "kind": "audio_transcription", "cid": 123,
        "provenance": {"cid": 123, "timestamp_kind": "vad_segment",
                       "automatically_verified_truth": False, "audio_sha256": "b" * 64,
                       "models": {"asr": "revision-a", "vad": "revision-b"}},
    }


class Generator:
    model_id = "generator"

    def __init__(self, mutate=None):
        self.mutate = mutate

    def complete(self, system, user):
        self.system, self.payload = system, json.loads(user)
        reply = {key: self.payload[key] for key in ("user_request", "source_ref", "payload_hash")}
        evidence = {"text": "打开设置", "segment_indices": [1], "start": 1.0, "end": 3.0}
        reply.update(methods=[{"title": "设置", "applicability": "有设置页面",
                               "steps": [{"order": 1, **evidence}],
                               "checkpoints": [evidence]}], unverified=[])
        if self.mutate:
            self.mutate(reply)
        return json.dumps(reply, ensure_ascii=False)


class Reviewer:
    model_id = "reviewer"

    def __init__(self, mutate=None):
        self.mutate = mutate

    def complete(self, system, user):
        self.system, self.payload = system, json.loads(user)
        reply = {key: self.payload[key] for key in (
            "reviewer_model_id", "draft_sha256", "payload_hash", "source_ref"
        )}
        reply.update(items=[{"location": item["location"], "verdict": "有依据",
                             "segment_indices": item["segment_indices"], "note": "转写支持"}
                            for item in self.payload["review_items"]],
                     problems=[], suggestion="supportable")
        if self.mutate:
            self.mutate(reply)
        return json.dumps(reply, ensure_ascii=False)


def build(ref=None, provider=None):
    return builder.build_method_list(SEGMENTS, REQUEST, ref or source_ref(), HASH,
                                     provider or Generator())


def test_asr_context_survives_build_render_review_and_hash_binding():
    ref, generator, reviewer = source_ref(), Generator(), Reviewer()
    draft = build(ref, generator)
    assert draft["source_ref"] == generator.payload["source_ref"] == ref
    assert builder.validate_method_list(draft, SEGMENTS, REQUEST, ref, HASH) == draft
    markdown = builder.render_markdown(draft)
    for text in (generator.system, markdown):
        assert "本机ASR" in text and "VAD" in text and "识别错误" in text
    review = quality.review_method_list(draft, SEGMENTS, REQUEST, ref, HASH, reviewer)
    assert reviewer.payload["source_ref"] == reviewer.payload["draft"]["source_ref"] == ref
    assert review["source_ref"] == ref and review["advisory_only"] is True
    assert "不能认证原音频" in reviewer.system
    assert quality.validate_review_candidate(review, draft, SEGMENTS, REQUEST, ref, HASH) == review
    changed = copy.deepcopy(draft)
    changed["source_ref"]["provenance"]["models"]["asr"] = "different"
    assert quality.draft_sha256(changed) != quality.draft_sha256(draft)
    with pytest.raises(quality.ArtifactReviewError):
        quality.review_method_list(changed, SEGMENTS, REQUEST, ref, HASH, Reviewer())


def test_asr_source_and_review_echo_cannot_drop_or_change_provenance():
    def corrupt(reply):
        reply["source_ref"]["provenance"]["models"]["vad"] = "changed"

    with pytest.raises(builder.ArtifactBuildError, match="source_ref_tampered"):
        build(provider=Generator(corrupt))
    draft = build()
    with pytest.raises(quality.ArtifactReviewError, match="review_identity_mismatch"):
        quality.review_method_list(draft, SEGMENTS, REQUEST, source_ref(), HASH, Reviewer(corrupt))
    review = quality.review_method_list(draft, SEGMENTS, REQUEST, source_ref(), HASH, Reviewer())
    del review["source_ref"]
    with pytest.raises(quality.ArtifactReviewError):
        quality.validate_review_candidate(review, draft, SEGMENTS, REQUEST, source_ref(), HASH)


@pytest.mark.parametrize("change", [
    {"kind": "unregistered"}, {"cid": True}, {"cid": 999},
    {"provenance": {"cid": 123, "timestamp_kind": "word",
                    "automatically_verified_truth": False}},
    {"provenance": {"cid": 123, "timestamp_kind": "vad_segment",
                    "automatically_verified_truth": True}},
])
def test_asr_invalid_identity_is_rejected_before_generation(change):
    ref = source_ref()
    ref.update(change)
    with pytest.raises(builder.ArtifactBuildError, match="invalid_source_ref"):
        build(ref)


def test_subtitle_source_shape_and_prompt_stay_compatible():
    ref, generator = {"bvid": "BV1AJen6SEVQ", "page": 42}, Generator()
    draft = build(ref, generator)
    assert draft["source_ref"] == ref
    assert generator.system == builder.SYSTEM_CONTRACT
    assert "本机ASR" not in builder.render_markdown(draft)
