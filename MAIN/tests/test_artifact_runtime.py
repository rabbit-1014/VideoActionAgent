"""A saved draft is readable, version-bound, and never a completion certificate."""

import json

import pytest
from sqlalchemy import text
from test_source_exit import prepared, sendable, settlement

from video_action_agent.artifact_builder import build_method_list
from video_action_agent.artifact_quality import review_method_list
from video_action_agent.runtime import Conflict, NotFound


class SmallDraftProvider:
    model_id = "test-draft-provider"

    def complete(self, _system, user):
        data = json.loads(user)
        first, second = data["segments"][:2]
        return json.dumps(
            {
                "user_request": data["user_request"],
                "source_ref": data["source_ref"],
                "payload_hash": data["payload_hash"],
                "methods": [
                    {
                        "title": "视频中的加字幕方法",
                        "applicability": "按视频演示的剪映界面操作",
                        "steps": [
                            {
                                "order": 1,
                                "text": "查看视频介绍的加字幕步骤",
                                "segment_indices": [1],
                                "start": first["start"],
                                "end": first["end"],
                            }
                        ],
                        "checkpoints": [
                            {
                                "text": "确认视频提到了三种方法",
                                "segment_indices": [2],
                                "start": second["start"],
                                "end": second["end"],
                            }
                        ],
                    }
                ],
                "unverified": [],
            },
            ensure_ascii=False,
        )


class SmallReviewProvider:
    model_id = "test-independent-reviewer"

    def complete(self, _system, user):
        payload = json.loads(user)
        return json.dumps({
            "reviewer_model_id": payload["reviewer_model_id"],
            "draft_sha256": payload["draft_sha256"],
            "payload_hash": payload["payload_hash"],
            "items": [
                {"location": item["location"], "verdict": "有依据",
                 "segment_indices": item["segment_indices"], "note": "字幕可支持"}
                for item in payload["review_items"]
            ],
            "problems": [], "suggestion": "supportable",
        }, ensure_ascii=False)


def settled_artifact_claim(runtime, engine):
    task, admission, attempt = prepared(runtime, engine)
    assert sendable(runtime, admission.claim, admission.operation_id, attempt["attempt_id"]).sendable
    runtime.settle_source_result(admission.claim, "alice", settlement(admission.operation_id))
    claim = runtime.claim("artifact-worker", {"build_artifact"}, task_id=task["task_id"])
    assert claim is not None, "a recorded subtitle material must have a claimable artifact successor"
    material = runtime.read_formal_materials("alice", task["task_id"])[0]
    draft = build_method_list(
        material["segments"],
        claim.request["message"],
        {"bvid": material["bvid"], "page": material["page"]},
        material["payload_hash"],
        SmallDraftProvider(),
    )
    return task, claim, draft


def test_artifact_candidate_is_saved_once_and_readable_only_by_current_owner(runtime, engine):
    task, claim, draft = settled_artifact_claim(runtime, engine)
    result = runtime.save_artifact_candidate(claim, "alice", draft)
    assert result["state"] == "unreviewed_candidate" and result["idempotent"] is False
    assert runtime.save_artifact_candidate(claim, "alice", draft) == {**result, "idempotent": True}

    saved = runtime.get_artifact("alice", task["task_id"])
    assert saved["artifact_id"] == result["artifact_id"]
    assert saved["draft"] == draft
    assert "方法清单（候选工作稿）" in saved["markdown"]
    assert saved["state"] == "unreviewed_candidate"
    assert runtime.get("alice", task["task_id"])["completed"] is False
    assert runtime.get("alice", task["task_id"])["formation"]["waiting"] is True
    with pytest.raises(NotFound):
        runtime.get_artifact("bob", task["task_id"])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM artifact_candidates")).scalar_one() == 1
        assert conn.execute(
            text("SELECT state FROM work_items WHERE work_id=:work"), {"work": claim.work_id}
        ).scalar_one() == "closed"


def test_cancel_or_changed_draft_cannot_replace_current_candidate(runtime, engine):
    task, claim, draft = settled_artifact_claim(runtime, engine)
    changed = {**draft, "user_request": "篡改用户请求"}
    with pytest.raises(Conflict):
        runtime.save_artifact_candidate(claim, "alice", changed)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM artifact_candidates")).scalar_one() == 0

    runtime.cancel("alice", task["task_id"], claim.revision)
    with pytest.raises(Conflict):
        runtime.save_artifact_candidate(claim, "alice", draft)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM artifact_candidates")).scalar_one() == 0


def test_only_current_owner_can_explicitly_accept_candidate(runtime, engine):
    task, claim, draft = settled_artifact_claim(runtime, engine)
    saved = runtime.save_artifact_candidate(claim, "alice", draft)
    revision = runtime.get("alice", task["task_id"])["revision"]
    with pytest.raises(NotFound):
        runtime.accept_artifact("bob", task["task_id"], saved["artifact_id"], revision)
    with pytest.raises(Conflict):
        runtime.accept_artifact("alice", task["task_id"], saved["artifact_id"], revision - 1)
    assert runtime.get("alice", task["task_id"])["completed"] is False
    accepted = runtime.accept_artifact(
        "alice", task["task_id"], saved["artifact_id"], revision
    )
    assert accepted["completed"] is True and accepted["idempotent"] is False
    assert runtime.accept_artifact(
        "alice", task["task_id"], saved["artifact_id"], revision
    )["idempotent"] is True
    assert runtime.get("alice", task["task_id"])["completed"] is True
    assert runtime.get_artifact("alice", task["task_id"])["owner_accepted"] is True


def test_independent_review_is_append_only_readable_and_never_completes(runtime, engine):
    task, claim, draft = settled_artifact_claim(runtime, engine)
    artifact = runtime.save_artifact_candidate(claim, "alice", draft)
    material = runtime.read_formal_materials("alice", task["task_id"])[0]
    review = review_method_list(
        draft, material["segments"], claim.request["message"],
        {"bvid": material["bvid"], "page": material["page"]},
        material["payload_hash"], SmallReviewProvider(),
    )
    revision = runtime.get("alice", task["task_id"])["revision"]

    saved = runtime.save_artifact_review(
        "alice", task["task_id"], artifact["artifact_id"], revision, review
    )
    assert saved["idempotent"] is False
    assert runtime.save_artifact_review(
        "alice", task["task_id"], artifact["artifact_id"], revision, review
    )["idempotent"] is True
    visible = runtime.get_artifact("alice", task["task_id"])
    assert visible["reviews"][0]["review"] == review
    assert visible["state"] == "unreviewed_candidate"
    assert visible["owner_accepted"] is False
    assert runtime.get("alice", task["task_id"])["completed"] is False
    changed = json.loads(json.dumps(review))
    changed["items"][0]["note"] = "第二次独立意见"
    second = runtime.save_artifact_review(
        "alice", task["task_id"], artifact["artifact_id"], revision, changed
    )
    assert second["review_id"] != saved["review_id"]
    assert len(runtime.get_artifact("alice", task["task_id"])["reviews"]) == 2
    with pytest.raises(NotFound):
        runtime.get_artifact("bob", task["task_id"])
    with pytest.raises(NotFound):
        runtime.save_artifact_review(
            "bob", task["task_id"], artifact["artifact_id"], revision, review
        )


def test_review_write_rejects_stale_or_mismatched_result(runtime, engine):
    task, claim, draft = settled_artifact_claim(runtime, engine)
    artifact = runtime.save_artifact_candidate(claim, "alice", draft)
    material = runtime.read_formal_materials("alice", task["task_id"])[0]
    review = review_method_list(
        draft, material["segments"], claim.request["message"],
        {"bvid": material["bvid"], "page": material["page"]},
        material["payload_hash"], SmallReviewProvider(),
    )
    revision = runtime.get("alice", task["task_id"])["revision"]
    forged = {**review, "draft_sha256": "0" * 64}
    with pytest.raises(Conflict):
        runtime.save_artifact_review(
            "alice", task["task_id"], artifact["artifact_id"], revision, forged
        )
    with pytest.raises(Conflict):
        runtime.save_artifact_review(
            "alice", task["task_id"], artifact["artifact_id"], revision - 1, review
        )
    assert runtime.get_artifact("alice", task["task_id"])["reviews"] == []
