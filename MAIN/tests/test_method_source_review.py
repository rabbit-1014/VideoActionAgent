"""DOWN-017 offline tests: declared method/field contract and transcript-role opinion.

Only three classes of cases: (1) declaration parsing and revocation, (2) the real source
failure with verbatim downgrade, a precisely located missing checkpoint and a disclosed
author inference, (3) cold re-read/tamper rejection with zero provider. No API, browser,
database or external model is touched.
"""

import copy
import hashlib
import json

import pytest

from video_action_agent.method_source_review import (
    MethodReviewError,
    evaluate_method_review,
    prepare_method_requirements,
    validate_method_requirements,
    validate_method_review,
)

REQUEST = "保留手动文本、识别字幕、文稿匹配三种方法，每种按“适用情况—步骤—检查点”组织。"

SECTIONS = [
    {
        "title": "方法一：手动新建文本",
        "items": [
            {"text": "在界面左上角文本一栏新建文本到时间轴", "segment_indices": [2],
             "start": 1.0, "end": 2.0, "label": "步骤", "layout": "step"},
            {"text": "边听音频手动打上字幕", "segment_indices": [2],
             "start": 1.0, "end": 2.0, "label": "步骤", "layout": "step"},
        ],
    },
    {
        "title": "方法二：识别字幕",
        "items": [
            {"text": "适合无现成文稿的视频", "segment_indices": [3],
             "start": 3.0, "end": 4.0, "label": "适用情况"},
            {"text": "需要联网", "segment_indices": [3],
             "start": 3.0, "end": 4.0, "label": "步骤"},
            {"text": "作者推断：识别结果取决于普通话水平", "segment_indices": [3],
             "start": 3.0, "end": 4.0, "label": "作者推断"},
        ],
    },
    {
        "title": "方法三：文稿匹配",
        "items": [
            {"text": "需要先有匹配的文稿", "segment_indices": [4],
             "start": 5.0, "end": 6.0, "label": "适用情况"},
        ],
    },
]

SEGMENTS = [
    {"start": 0.5, "end": 1.0, "text": "先用剪映打开需要加字幕的视频"},
    {"start": 1.0, "end": 2.0, "text": "在界面左上角文本一栏新建文本到时间轴边听音频手动打上字幕"},
    {"start": 3.0, "end": 4.0, "text": "识别字幕需要联网"},
    {"start": 5.0, "end": 6.0, "text": "文稿匹配需要先有匹配的文稿"},
]


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
    """Offline stub; never reaches a network or a real model."""

    def __init__(self, build, mutate=None):
        self.build = build
        self.mutate = mutate
        self.calls = 0
        self.bodies = []

    def complete(self, system, user):
        self.calls += 1
        body = json.loads(user)
        self.bodies.append(body)
        reply = self.build(body)
        if self.mutate is not None:
            self.mutate(reply)
        return json.dumps(reply, ensure_ascii=False)


class BoomProvider:
    def complete(self, system, user):  # pragma: no cover - must never run
        raise AssertionError("provider must not be called by cold validation")


def _reply(body):
    return {
        "requirements_fingerprint": body["requirements_fingerprint"],
        "candidate_fingerprint": body["candidate_fingerprint"],
        "source_fingerprint": body["source_fingerprint"],
        "bindings": [
            {"method_id": "m001", "section_index": 1, "explanation": "方法一对应第一章"},
            {"method_id": "m002", "section_index": 2, "explanation": "方法二对应第二章"},
            {"method_id": "m003", "section_index": 3, "explanation": "方法三对应第三章"},
        ],
        "cells": [
            {"method_id": "m001", "field": "applicability", "item_indices": [],
             "explanation": "方法一没有适用情况条目"},
            {"method_id": "m001", "field": "steps", "item_indices": [1, 2],
             "explanation": "两个步骤"},
            {"method_id": "m001", "field": "checkpoint", "item_indices": [],
             "explanation": "方法一没有检查点条目"},
            {"method_id": "m002", "field": "applicability", "item_indices": [1],
             "explanation": "适用情况"},
            {"method_id": "m002", "field": "steps", "item_indices": [2],
             "explanation": "步骤"},
            {"method_id": "m002", "field": "checkpoint", "item_indices": [3],
             "explanation": "检查点"},
            {"method_id": "m003", "field": "applicability", "item_indices": [1],
             "explanation": "适用情况"},
            {"method_id": "m003", "field": "steps", "item_indices": [1],
             "explanation": "只有一个步骤描述"},
            {"method_id": "m003", "field": "checkpoint", "item_indices": [],
             "explanation": "方法三没有检查点条目"},
        ],
        "items": [
            {"section_index": 1, "item_index": 1, "clauses": [
                {"text": "在界面左上角文本一栏新建文本到时间轴", "role": "verbatim",
                 "source_quotes": [{"index": 2, "text": "在界面左上角文本一栏新建文本到时间轴"}]}]},
            {"section_index": 1, "item_index": 2, "clauses": [
                {"text": "边听音频手动打上字幕", "role": "verbatim",
                 "source_quotes": [{"index": 2, "text": "边听音频手动打上字幕"}]}]},
            {"section_index": 2, "item_index": 1, "clauses": [
                {"text": "适合无现成文稿的视频", "role": "verbatim",
                 "source_quotes": [{"index": 3, "text": "识别字幕需要联网"}]}]},
            {"section_index": 2, "item_index": 2, "clauses": [
                {"text": "需要联网", "role": "verbatim",
                 "source_quotes": [{"index": 3, "text": "识别字幕需要联网"}]}]},
            {"section_index": 2, "item_index": 3, "clauses": [
                {"text": "作者推断：识别结果取决于普通话水平", "role": "inference",
                 "source_quotes": []}]},
            {"section_index": 3, "item_index": 1, "clauses": [
                {"text": "需要先有匹配的文稿", "role": "verbatim",
                 "source_quotes": [{"index": 4, "text": "文稿匹配需要先有匹配的文稿"}]}]},
        ],
    }


def _requirements():
    return prepare_method_requirements(REQUEST, [])


def _evaluate(mutate=None):
    return evaluate_method_review(_requirements(), SECTIONS, SEGMENTS, Provider(_reply, mutate))


def _finding(saved, method_id, field):
    for finding in saved["findings"]:
        if finding["method_id"] == method_id and finding["field"] == field:
            return finding
    raise AssertionError("finding not found")


def test_declaration_nine_cells_two_methods_and_non_explicit_rejection():
    value = prepare_method_requirements(REQUEST, [])
    assert value["schema_version"] == 2
    assert value["status"] == "declared_method_fields"
    assert value["scope"] == "declared_method_fields_and_text_roles"
    assert value["user_request"] == REQUEST and value["amendments"] == []
    assert [m["id"] for m in value["profile"]["methods"]] == ["m001", "m002", "m003"]
    assert [m["name"] for m in value["profile"]["methods"]] == [
        "手动文本", "识别字幕", "文稿匹配"]
    assert [f["key"] for f in value["profile"]["fields"]] == [
        "applicability", "steps", "checkpoint"]
    assert [f["label"] for f in value["profile"]["fields"]] == ["适用情况", "步骤", "检查点"]
    assert value["profile"]["basis"] == {
        "version": 0,
        "quote": "保留手动文本、识别字幕、文稿匹配三种方法，"
                 "每种按“适用情况—步骤—检查点”组织"}
    assert len(value["requirements"]) == 9
    assert [r["id"] for r in value["requirements"]] == [f"r{i:03d}" for i in range(1, 10)]
    first = value["requirements"][0]
    assert first["kind"] == "content" and first["method_id"] == "m001"
    assert first["field"] == "applicability" and first["text"] == "手动文本：适用情况"
    assert first["basis"] == [{"version": 0, "quote": "手动文本"},
                              {"version": 0, "quote": "适用情况"}]
    assert value["request_fingerprint"] == _fingerprint(
        {"user_request": REQUEST, "amendments": []})
    payload = {key: item for key, item in value.items()
               if key != "requirements_fingerprint"}
    assert value["requirements_fingerprint"] == _fingerprint(payload)
    assert validate_method_requirements(value, REQUEST, []) == value

    # A different pair of method names, alternative labels and separators.
    other = "保留字幕样式、批量导出两种方法，每种按“适用条件-操作步骤-校验点”组织。"
    value2 = prepare_method_requirements(other, [])
    assert [m["name"] for m in value2["profile"]["methods"]] == ["字幕样式", "批量导出"]
    assert [f["key"] for f in value2["profile"]["fields"]] == [
        "applicability", "steps", "checkpoint"]
    assert len(value2["requirements"]) == 6
    assert validate_method_requirements(value2, other, []) == value2

    # Only the last amendment is read; a later revocation yields None.
    first_decl = {"version": 1, "instruction": REQUEST}
    assert prepare_method_requirements("整理这段视频。", [first_decl]) is not None
    revoked = [first_decl, {"version": 2, "instruction": "删除检查点，其余不变。"}]
    assert prepare_method_requirements("整理这段视频。", revoked) is None
    replacement = [first_decl, {"version": 2, "instruction": other}]
    rep = prepare_method_requirements("整理这段视频。", replacement)
    assert rep["profile"]["basis"]["version"] == 2
    assert [m["name"] for m in rep["profile"]["methods"]] == ["字幕样式", "批量导出"]

    # Non-explicit or inconsistent declarations are None, never guessed.
    assert prepare_method_requirements("把视频整理成课程笔记。", []) is None
    assert prepare_method_requirements(
        "保留A、B三种方法，每种按“适用情况—步骤—检查点”组织。", []) is None
    assert prepare_method_requirements(
        "保留A、A、B三种方法，每种按“适用情况—步骤—检查点”组织。", []) is None
    assert prepare_method_requirements(
        "保留A、B两种方法，每种按“适用情况—步骤”组织。", []) is None
    for text in ("保留A、B两种方法，标题按“适用情况—步骤—检查点”组织。",
                 "不要保留A、B两种方法，每种按“适用情况—步骤—检查点”组织。",
                 "保留A、B两种方法，每种按“适用情况—步骤—检查点”删除。",
                 "保留A、B、C三种方法，每种按“适用情况—步骤—检查点”组织；不要保留C。",
                 "保留A、B两种方法，每种按“适用情况—步骤—检查点”组织。删除检查点。"):
        assert prepare_method_requirements(text, []) is None

    # Bad input still raises instead of returning None.
    with pytest.raises(MethodReviewError, match="invalid_requirements_input"):
        prepare_method_requirements("", [])
    with pytest.raises(MethodReviewError, match="invalid_requirements_input"):
        prepare_method_requirements(123, [])
    with pytest.raises(MethodReviewError, match="invalid_requirements_input"):
        prepare_method_requirements("整理。", [{"version": 2, "instruction": "x"},
                                               {"version": 1, "instruction": "y"}])


def test_real_source_failure_verbatim_downgrade_and_missing_checkpoint():
    requirements = _requirements()
    saved = _evaluate()

    steps = _finding(saved, "m001", "steps")
    assert steps["coverage"] == "provided" and steps["status"] == "met"
    assert steps["source_support"] == "supported"
    assert steps["source_indices"] == [2]

    # A clause claiming verbatim while the cited transcript does not contain it.
    applicability = _finding(saved, "m002", "applicability")
    assert applicability["coverage"] == "provided"
    assert applicability["status"] == "uncertain"
    assert applicability["source_support"] == "uncertain"
    clause = applicability["role_details"][0]["clauses"][0]
    assert clause["reported_role"] == "verbatim"
    assert clause["effective_role"] == "unverified_interpretation"
    assert clause["disclosed"] is False

    # A disclosed author inference stays provided and is never treated as forgery.
    checkpoint = _finding(saved, "m002", "checkpoint")
    assert checkpoint["coverage"] == "provided" and checkpoint["status"] == "met"
    assert checkpoint["source_support"] == "not_applicable"
    inference = checkpoint["role_details"][0]["clauses"][0]
    assert inference["reported_role"] == "inference"
    assert inference["effective_role"] == "author_inference"
    assert inference["disclosed"] is True

    # Method one has no checkpoint item at all: located exactly, unmet.
    missing = _finding(saved, "m001", "checkpoint")
    assert missing["coverage"] == "missing" and missing["status"] == "unmet"
    assert missing["source_support"] == "missing"
    assert missing["candidate_excerpt"] == "" and missing["role_details"] == []
    assert _finding(saved, "m003", "checkpoint")["status"] == "unmet"
    assert saved["outcome"] == "unmet"
    assert saved["quality_approved"] is False and saved["completed"] is False
    assert saved["formal_task"] is False and saved["other_requirements_reviewed"] is False
    assert saved["candidate_fingerprint"] == _fingerprint(SECTIONS)
    assert saved["source_fingerprint"] == _fingerprint(SEGMENTS)
    assert validate_method_review(saved, requirements, SECTIONS, SEGMENTS) == saved
    assert validate_method_review(saved, requirements, SECTIONS, SEGMENTS,
                                  ) == saved  # no provider argument exists

    # A valid unbound method is uncertainty, not a crash or leaked prior roles.
    def unbound(reply):
        reply["bindings"][0]["section_index"] = None
        for cell in reply["cells"]:
            if cell["method_id"] == "m001":
                cell["item_indices"] = []
        reply["items"] = [item for item in reply["items"] if item["section_index"] != 1]
    absent = _evaluate(unbound)
    assert _finding(absent, "m001", "steps")["coverage"] == "uncertain"
    assert _finding(absent, "m001", "steps")["role_details"] == []
    assert validate_method_review(absent, requirements, SECTIONS, SEGMENTS) == absent

    def bad(mutate, code):
        with pytest.raises(MethodReviewError) as info:
            _evaluate(mutate)
        assert info.value.code == code

    bad(lambda r: r["bindings"][1].update(section_index=1), "invalid_binding_coverage")
    bad(lambda r: r["bindings"][1].update(section_index=9), "invalid_binding_coverage")
    bad(lambda r: r["bindings"][2].update(section_index=None), "invalid_cell_coverage")
    bad(lambda r: r["cells"].pop(), "invalid_cell_coverage")
    bad(lambda r: r["cells"][0].update(item_indices=[3]), "invalid_cell_coverage")
    bad(lambda r: r["cells"][0].update(field="unknown"), "invalid_cell_coverage")
    bad(lambda r: r["items"].pop(), "invalid_item_coverage")
    bad(lambda r: r["items"].append(copy.deepcopy(r["items"][0])), "invalid_item_coverage")
    bad(lambda r: r["items"][0]["clauses"][0]["source_quotes"][0].update(
        text="不存在的转写"), "invalid_citation")
    bad(lambda r: r["items"][0]["clauses"][0]["source_quotes"][0].update(
        index=9), "invalid_citation")
    bad(lambda r: r["items"][0]["clauses"][0].update(role="verdict"),
        "invalid_review_reply")
    bad(lambda r: r["items"][0]["clauses"][0].update(text="删改后的文字"),
        "invalid_item_fulltext")
    bad(lambda r: r.update(candidate_fingerprint="0" * 64), "review_identity_mismatch")
    bad(lambda r: r.update(extra="field"), "invalid_review_reply")
    bad(lambda r: r["cells"][0].update(explanation="  "), "invalid_review_reply")

    with pytest.raises(MethodReviewError, match="invalid_provider"):
        evaluate_method_review(requirements, SECTIONS, SEGMENTS, object())
    with pytest.raises(MethodReviewError, match="invalid_review_input"):
        evaluate_method_review(requirements, [], SEGMENTS, Provider(_reply))
    with pytest.raises(MethodReviewError, match="invalid_review_input"):
        evaluate_method_review(requirements, SECTIONS,
                               [{"start": 2.0, "end": 1.0, "text": "x"}],
                               Provider(_reply))

    # The provider sees only text/labels/layout and program indices.
    provider = Provider(_reply)
    evaluate_method_review(requirements, SECTIONS, SEGMENTS, provider)
    body = provider.bodies[0]
    assert body["requirements"] == requirements
    assert set(body["sections"][0]) == {"index", "title", "items"}
    assert set(body["sections"][0]["items"][0]) == {"index", "text", "label", "layout"}
    assert set(body["segments"][0]) == {"index", "text"}
    assert body["sections"][0]["index"] == 1 and body["sections"][0]["items"][0]["index"] == 1
    assert body["segments"][0]["index"] == 1


def test_saved_cold_read_tamper_rejection_and_zero_provider():
    requirements = _requirements()
    provider = Provider(_reply)
    saved = evaluate_method_review(requirements, SECTIONS, SEGMENTS, provider)
    assert provider.calls == 1

    # Cold re-read works from the stored explicit fields, even without the raw reply.
    no_reply = copy.deepcopy(saved)
    no_reply.pop("reply")
    assert validate_method_review(no_reply, requirements, SECTIONS, SEGMENTS) == no_reply
    assert validate_method_review(saved, requirements, SECTIONS, SEGMENTS) == saved
    assert provider.calls == 1  # validation never calls the provider

    def bad(mutate, code):
        tampered = copy.deepcopy(saved)
        mutate(tampered)
        with pytest.raises(MethodReviewError) as info:
            validate_method_review(tampered, requirements, SECTIONS, SEGMENTS)
        assert info.value.code == code

    bad(lambda v: v.update(quality_approved=True), "invalid_review_value")
    bad(lambda v: v.update(completed=True), "invalid_review_value")
    bad(lambda v: v.update(formal_task=True), "invalid_review_value")
    bad(lambda v: v.update(status="accepted"), "invalid_review_value")
    bad(lambda v: v.update(schema_version=3), "invalid_review_value")
    bad(lambda v: v.update(extra="field"), "invalid_review_value")
    bad(lambda v: v["bindings"][0].update(section_index=2), "invalid_binding_coverage")
    bad(lambda v: v["findings"][0].update(status="met"), "review_candidate_mismatch")
    bad(lambda v: v["findings"][0].update(coverage="provided"), "review_candidate_mismatch")
    bad(lambda v: v["findings"][0].update(source_support="supported"),
        "review_candidate_mismatch")
    bad(lambda v: v["findings"][0].update(requirement_id="r999"), "review_candidate_mismatch")
    bad(lambda v: v["findings"][3]["role_details"][0]["clauses"][0].update(
        effective_role="transcript_match"), "review_candidate_mismatch")
    bad(lambda v: v["reviewed_items"][2]["clauses"][0].update(reported_role="paraphrase"),
        "review_candidate_mismatch")
    bad(lambda v: v["reviewed_items"][2]["clauses"][0].update(text="篡改的条目"),
        "invalid_item_fulltext")
    bad(lambda v: v["reviewed_items"][2]["clauses"][0]["source_quotes"][0].update(
        text="伪造引用"), "invalid_citation")
    bad(lambda v: v["findings"][3]["role_details"][0]["clauses"][0].update(
        effective_role="author_inference"), "review_candidate_mismatch")
    bad(lambda v: v["findings"][0].update(source_indices=[99]), "review_candidate_mismatch")
    bad(lambda v: v["findings"][0].update(candidate_excerpt="伪造内容"),
        "review_candidate_mismatch")

    # Requirements tamper and cold requirements validation.
    assert validate_method_requirements(requirements, REQUEST, []) == requirements
    tampered = copy.deepcopy(requirements)
    tampered["requirements"][0]["text"] = "被替换的要求"
    _refreeze(tampered)
    with pytest.raises(MethodReviewError, match="method_requirements_mismatch"):
        validate_method_requirements(tampered, REQUEST, [])
    tampered2 = copy.deepcopy(requirements)
    tampered2["profile"]["methods"][0]["name"] = "改名"
    with pytest.raises(MethodReviewError):
        validate_method_requirements(tampered2, REQUEST, [])
    with pytest.raises(MethodReviewError, match="invalid_method_requirements"):
        validate_method_requirements({"schema_version": 2}, REQUEST, [])
    with pytest.raises(MethodReviewError, match="method_requirements_mismatch"):
        validate_method_requirements(requirements, "另一个目标。", [])

    # A fresh validation object with a provider that would explode confirms no call.
    assert validate_method_review(saved, requirements, SECTIONS, SEGMENTS) == saved
    assert BoomProvider  # referenced so the sentinel is not an unused symbol
