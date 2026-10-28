import hashlib
import json
from copy import deepcopy

import pytest

from video_action_agent.notes_requirements import (
    NotesRequirementsError,
    evaluate_candidate,
    freeze_requirements,
    validate_evaluation,
    validate_requirements,
)

REQUEST = "请把视频整理成课程笔记，并给出三种方法，每种方法都要有检查点。"
AMENDMENTS = [
    {"version": 1, "instruction": "阅读稿用中文，按主题分章。"},
    {"version": 2, "instruction": "不要逐段复述时序。"},
]
SEGMENTS = [{"start": 1.0, "end": 3.0, "text": "方法一讲解"},
            {"start": 5.0, "end": 7.5, "text": "方法二与方法三"}]
CANDIDATE = "本课程给出方法一、方法二、方法三；每种方法都有检查点，按主题分章。"


def _fingerprint(value):
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _refreeze(value):
    payload = {key: item for key, item in value.items()
               if key != "requirements_fingerprint"}
    value["requirements_fingerprint"] = _fingerprint(payload)
    return value


class Provider:
    """Offline stub: only returns a reply built from the request body."""

    def __init__(self, build):
        self.build = build
        self.bodies = []

    def complete(self, system, user):
        body = json.loads(user)
        self.bodies.append(body)
        return self.build(body)


def freeze_builder(mutate=None):
    def build(body):
        base = {
            "request_fingerprint": body["request_fingerprint"],
            "requirements": [
                {"kind": "content", "text": "三种方法各含检查点",
                 "basis": [{"version": 0, "quote": "三种方法，每种方法都要有检查点"}]},
                {"kind": "presentation", "text": "中文按主题分章",
                 "basis": [{"version": 1, "quote": "中文，按主题分章"}]},
                {"kind": "boundary", "text": "不要逐段复述时序",
                 "basis": [{"version": 2, "quote": "不要逐段复述时序"}]},
            ],
        }
        if mutate is not None:
            mutate(base)
        return json.dumps(base, ensure_ascii=False)

    return build


def eval_builder(mutate=None):
    def build(body):
        base = {
            "request_fingerprint": body["request_fingerprint"],
            "requirements_fingerprint": body["requirements_fingerprint"],
            "candidate_fingerprint": body["candidate_fingerprint"],
            "source_fingerprint": body["source_fingerprint"],
            "findings": [
                {"requirement_id": "r001", "status": "met", "explanation": "正文覆盖",
                 "candidate_excerpt": "每种方法都有检查点", "source_indices": [1],
                 "source_support": "supported"},
                {"requirement_id": "r002", "status": "met", "explanation": "按主题分章",
                 "candidate_excerpt": "", "source_indices": [],
                 "source_support": "not_applicable"},
                {"requirement_id": "r003", "status": "met", "explanation": "未见逐段复述",
                 "candidate_excerpt": "", "source_indices": [],
                 "source_support": "not_applicable"},
            ],
        }
        if mutate is not None:
            mutate(base)
        return json.dumps(base, ensure_ascii=False)

    return build


def _freeze():
    return freeze_requirements(REQUEST, deepcopy(AMENDMENTS), Provider(freeze_builder()))


def _evaluate(frozen, mutate=None):
    return evaluate_candidate(frozen, CANDIDATE, SEGMENTS, Provider(eval_builder(mutate)))


def test_two_stage_isolation_frozen_quotes_and_real_citations():
    freeze_provider = Provider(freeze_builder())
    frozen = freeze_requirements(REQUEST, AMENDMENTS, freeze_provider)

    # Stage one saw the request text only, never the candidate or the source.
    assert set(freeze_provider.bodies[0]) == {"request_fingerprint", "user_request",
                                              "amendments"}
    assert freeze_provider.bodies[0]["user_request"] == REQUEST
    assert AMENDMENTS == [{"version": 1, "instruction": "阅读稿用中文，按主题分章。"},
                          {"version": 2, "instruction": "不要逐段复述时序。"}]

    assert frozen["schema_version"] == 1
    assert frozen["status"] == "model_derived_requirements"
    assert frozen["user_request"] == REQUEST
    assert frozen["amendments"] == AMENDMENTS
    assert [item["id"] for item in frozen["requirements"]] == ["r001", "r002", "r003"]
    assert [item["kind"] for item in frozen["requirements"]] == [
        "content", "presentation", "boundary"]
    assert frozen["request_fingerprint"] == _fingerprint(
        {"user_request": REQUEST, "amendments": AMENDMENTS})
    payload = {key: value for key, value in frozen.items()
               if key != "requirements_fingerprint"}
    assert frozen["requirements_fingerprint"] == _fingerprint(payload)
    assert validate_requirements(frozen, REQUEST, AMENDMENTS) == frozen

    before = deepcopy(frozen)
    eval_provider = Provider(eval_builder())
    saved = evaluate_candidate(frozen, CANDIDATE, SEGMENTS, eval_provider)
    body = eval_provider.bodies[0]
    assert body["requirements"] == frozen and body["candidate_text"] == CANDIDATE
    assert body["segments"][0]["index"] == 1
    assert frozen == before  # the frozen requirements were not modified

    assert saved["schema_version"] == 1 and saved["status"] == "model_opinion"
    assert saved["outcome"] == "all_reported_met"
    assert saved["quality_approved"] is False and saved["completed"] is False
    assert saved["formal_task"] is False
    assert saved["candidate_fingerprint"] == hashlib.sha256(
        CANDIDATE.encode("utf-8")).hexdigest()
    assert saved["source_fingerprint"] == _fingerprint(SEGMENTS)
    assert saved["findings"][0]["source_indices"] == [1]
    assert saved["findings"][0]["candidate_excerpt"] == "每种方法都有检查点"
    assert validate_evaluation(saved, frozen, CANDIDATE, SEGMENTS) == saved


def test_bad_fingerprints_citations_ids_excerpts_and_unknown_fields_rejected():
    frozen = _freeze()

    def bad_freeze(mutate, code="invalid_requirements_reply"):
        with pytest.raises(NotesRequirementsError) as info:
            freeze_requirements(REQUEST, AMENDMENTS, Provider(freeze_builder(mutate)))
        assert info.value.code == code

    bad_freeze(lambda value: value.update(request_fingerprint="0" * 64),
               "requirements_identity_mismatch")
    bad_freeze(lambda value: value.update(requirements=[]))

    def blank_text(value):
        value["requirements"][0]["text"] = "   "

    bad_freeze(blank_text)
    bad_freeze(lambda value: value["requirements"][0]["basis"][0].update(
        quote="不存在的原话"), "invalid_basis_quote")
    bad_freeze(lambda value: value["requirements"][0]["basis"][0].update(version=9),
               "invalid_basis_quote")
    bad_freeze(lambda value: value.update(extra="field"))

    def duplicate_keys(body):
        fingerprint = body["request_fingerprint"]
        return ('{"request_fingerprint":"' + fingerprint + '","request_fingerprint":"' +
                fingerprint + '","requirements":[]}')

    for raw in (duplicate_keys, lambda body: '{"x": NaN}', lambda body: "not json",
                lambda body: "```json\n{}\n```"):
        with pytest.raises(NotesRequirementsError, match="invalid_requirements_reply"):
            freeze_requirements(REQUEST, AMENDMENTS, Provider(raw))
    with pytest.raises(NotesRequirementsError, match="invalid_provider"):
        freeze_requirements(REQUEST, AMENDMENTS, object())

    def bad_eval(mutate, code):
        with pytest.raises(NotesRequirementsError) as info:
            _evaluate(frozen, mutate)
        assert info.value.code == code

    bad_eval(lambda value: value.update(candidate_fingerprint="1" * 64),
             "evaluation_identity_mismatch")
    bad_eval(lambda value: value.update(requirements_fingerprint="2" * 64),
             "evaluation_identity_mismatch")
    bad_eval(lambda value: value.update(request_fingerprint="3" * 64),
             "evaluation_identity_mismatch")
    bad_eval(lambda value: value.update(source_fingerprint="4" * 64),
             "evaluation_identity_mismatch")
    bad_eval(lambda value: value.update(findings=value["findings"][:2]),
             "invalid_finding_coverage")
    bad_eval(lambda value: value.update(findings=value["findings"] +
                                        [deepcopy(value["findings"][0])]),
             "invalid_finding_coverage")
    bad_eval(lambda value: value["findings"][0].update(requirement_id="r009"),
             "invalid_finding_coverage")
    bad_eval(lambda value: value["findings"][0].update(source_indices=[5]),
             "invalid_citation")
    bad_eval(lambda value: value["findings"][0].update(candidate_excerpt="稿中没有的引文"),
             "invalid_excerpt")
    bad_eval(lambda value: value["findings"][0].update(extra="field"),
             "invalid_evaluation_reply")
    with pytest.raises(NotesRequirementsError, match="invalid_provider"):
        evaluate_candidate(frozen, CANDIDATE, SEGMENTS, object())

    saved = _evaluate(frozen)

    def bad_saved(mutate, code):
        tampered = deepcopy(saved)
        mutate(tampered)
        with pytest.raises(NotesRequirementsError) as info:
            validate_evaluation(tampered, frozen, CANDIDATE, SEGMENTS)
        assert info.value.code == code

    bad_saved(lambda value: value.update(quality_approved=True),
              "evaluation_candidate_mismatch")
    bad_saved(lambda value: value.update(status="accepted"),
              "evaluation_candidate_mismatch")
    bad_saved(lambda value: value.update(schema_version=2),
              "evaluation_candidate_mismatch")
    bad_saved(lambda value: value.update(extra="field"),
              "evaluation_candidate_mismatch")
    bad_saved(lambda value: value["findings"][0].update(candidate_excerpt="伪引用"),
              "invalid_excerpt")
    bad_saved(lambda value: value["findings"][0].update(source_indices=[9]),
              "invalid_citation")

    def strip_content_evidence(value):
        value["findings"][0].update(candidate_excerpt="", source_indices=[],
                                    source_support="supported")

    conservative = _evaluate(frozen, strip_content_evidence)
    assert conservative["findings"][0]["status"] == "uncertain"

    # A stored content verdict may be downgraded but never upgraded to "met".
    disguised = deepcopy(conservative)
    disguised["findings"][0]["status"] = "met"
    disguised["outcome"] = "all_reported_met"
    with pytest.raises(NotesRequirementsError, match="evaluation_candidate_mismatch"):
        validate_evaluation(disguised, frozen, CANDIDATE, SEGMENTS)
    with pytest.raises(NotesRequirementsError, match="evaluation_identity_mismatch"):
        validate_evaluation(saved, frozen, "另一篇稿", SEGMENTS)

    tampered = deepcopy(frozen)
    tampered["requirements"][0]["text"] = "被替换的要求"
    with pytest.raises(NotesRequirementsError, match="requirements_identity_mismatch"):
        validate_requirements(tampered, REQUEST, AMENDMENTS)
    tampered = _refreeze(deepcopy(frozen))
    tampered["requirements"][1]["id"] = "r009"
    _refreeze(tampered)
    with pytest.raises(NotesRequirementsError, match="requirements_candidate_mismatch"):
        validate_requirements(tampered, REQUEST, AMENDMENTS)
    with pytest.raises(NotesRequirementsError, match="requirements_identity_mismatch"):
        validate_requirements(frozen, "另一个目标", AMENDMENTS)


def test_label_presence_without_content_coverage_is_not_all_reported_met():
    frozen = _freeze()

    def content_met_without_evidence(value):
        value["findings"][0].update(candidate_excerpt="", source_indices=[],
                                    source_support="supported")

    degraded = _evaluate(frozen, content_met_without_evidence)
    assert degraded["findings"][0]["status"] == "uncertain"
    assert degraded["outcome"] == "uncertain"
    assert validate_evaluation(degraded, frozen, CANDIDATE, SEGMENTS) == degraded

    def content_met_with_missing_source(value):
        value["findings"][0].update(source_support="missing")

    missing = _evaluate(frozen, content_met_with_missing_source)
    assert missing["findings"][0]["status"] == "uncertain"
    assert missing["outcome"] == "uncertain"
    assert missing["findings"][0]["candidate_excerpt"] == "每种方法都有检查点"

    def content_unmet(value):
        value["findings"][0].update(status="unmet", candidate_excerpt="",
                                    source_indices=[], source_support="missing")

    unmet = _evaluate(frozen, content_unmet)
    assert unmet["outcome"] == "unmet"
    assert validate_evaluation(unmet, frozen, CANDIDATE, SEGMENTS) == unmet

    # Presentation may stay met without invented citations, but a content gap still
    # keeps the whole outcome away from all_reported_met.
    def content_uncertain_presentation_met(value):
        value["findings"][0].update(status="uncertain", candidate_excerpt="",
                                    source_indices=[], source_support="uncertain")

    mixed = _evaluate(frozen, content_uncertain_presentation_met)
    assert mixed["outcome"] == "uncertain"
    assert mixed["findings"][1]["status"] == "met"
    assert mixed["findings"][1]["candidate_excerpt"] == ""
    assert mixed["findings"][1]["source_indices"] == []
    assert validate_evaluation(mixed, frozen, CANDIDATE, SEGMENTS) == mixed

    blank = _evaluate(frozen, content_met_without_evidence)
    assert blank["outcome"] != "all_reported_met"
