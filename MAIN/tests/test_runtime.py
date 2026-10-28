from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import event, text

REQUEST = {"schema_version": 1, "message": "Organize this tutorial", "source": "BV1gD421u75J"}


def submit(runtime, key="one", owner="alice"):
    return runtime.submit(owner, key, REQUEST)


def test_admission_is_persistent_and_not_completion(runtime, engine):
    task = submit(runtime)
    from video_action_agent.runtime import Runtime

    restored = Runtime(engine).get("alice", task["task_id"])
    assert restored["contract_state"] == "forming"
    assert restored["revision"] == 1
    assert restored["completed"] is False
    assert restored["work_state"] == "ready"


def test_task_and_enqueue_rollback_together(runtime, engine):
    def break_enqueue(conn, cursor, statement, params, context, many):
        if statement.lstrip().startswith("INSERT INTO work_items"):
            raise RuntimeError("injected enqueue failure")

    event.listen(engine, "before_cursor_execute", break_enqueue)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            submit(runtime)
    finally:
        event.remove(engine, "before_cursor_execute", break_enqueue)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM task_contracts")).scalar_one() == 0


def test_concurrent_admission_is_idempotent(runtime, engine):
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: submit(runtime), range(6)))
    assert len({r["task_id"] for r in results}) == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM work_items")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM transitions")).scalar_one() == 1


def test_idempotency_key_cannot_change_input(runtime):
    from video_action_agent.runtime import Conflict

    submit(runtime)
    with pytest.raises(Conflict):
        runtime.submit("alice", "one", {**REQUEST, "message": "Different request"})


def test_legacy_request_hash_stays_stable_without_local_browser_scope(runtime, engine):
    from video_action_agent.runtime import digest

    task = submit(runtime)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT request_hash FROM tasks WHERE task_id=:task"),
            {"task": task["task_id"]},
        ).scalar_one()
    assert row == digest(REQUEST)
    assert (
        runtime.submit("alice", "one", {**REQUEST, "local_browser_actions": []})["task_id"]
        == task["task_id"]
    )


def test_ownership_isolation(runtime):
    from video_action_agent.runtime import NotFound

    task = submit(runtime)
    with pytest.raises(NotFound):
        runtime.get("bob", task["task_id"])
    with pytest.raises(NotFound):
        runtime.cancel("bob", task["task_id"], 1)
    other = submit(runtime, owner="bob")
    assert other["task_id"] != task["task_id"]


def test_cancel_before_claim_suppresses_work(runtime):
    task = submit(runtime)
    canceled = runtime.cancel("alice", task["task_id"], 1)
    assert canceled["contract_state"] == "canceled"
    assert runtime.claim("w1", {"form_goal"}) is None
    assert runtime.cancel("alice", task["task_id"], 1)["revision"] == 2


def test_competing_workers_get_only_one_lease(runtime):
    submit(runtime)
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda n: runtime.claim(str(n), {"form_goal"}), range(6)))
    assert sum(c is not None for c in claims) == 1


def test_unregistered_work_not_claimed(runtime):
    submit(runtime)
    assert runtime.claim("w1", set()) is None
    assert runtime.claim("w1", {"send_email"}) is None


def test_receipt_does_not_complete_task(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    assert runtime.record(claim, {"schema_version": 1, "note": "test-only"}) == "recorded"
    assert runtime.record(claim, {"schema_version": 1, "note": "test-only"}) == "recorded"
    assert runtime.get("alice", task["task_id"])["completed"] is False
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM work_receipts")).scalar_one() == 1


def test_changed_duplicate_receipt_rejected(runtime):
    from video_action_agent.runtime import Conflict

    submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.record(claim, {"schema_version": 1, "x": 1})
    with pytest.raises(Conflict):
        runtime.record(claim, {"schema_version": 1, "x": 2})


def test_cancel_fences_late_result_but_retains_receipt(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.cancel("alice", task["task_id"], 1)
    assert runtime.record(claim, {"schema_version": 1, "note": "late"}) == "stale"
    view = runtime.get("alice", task["task_id"])
    assert view["contract_state"] == "canceled"
    assert view["revision"] == 2
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM work_receipts")).scalar_one() == 1


def test_revision_fences_old_worker_and_keeps_history(runtime, engine):
    from video_action_agent.runtime import Conflict

    task = submit(runtime)
    old = runtime.claim("old", {"form_goal"})
    revised = runtime.revise("alice", task["task_id"], 1, {**REQUEST, "message": "New goal"})
    assert revised["revision"] == 2
    assert runtime.record(old, {"schema_version": 1}) == "stale"
    fresh = runtime.claim("new", {"form_goal"})
    assert fresh.revision == 2
    with pytest.raises(Conflict):
        runtime.revise("alice", task["task_id"], 1, REQUEST)
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT state FROM task_contracts ORDER BY version")
        ).scalars().all() == ["superseded", "forming"]


def test_lease_expiry_is_not_permission_to_rerun(runtime, engine):
    submit(runtime)
    old = runtime.claim("old", {"form_goal"})
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    assert runtime.claim("normal", {"form_goal"}) is None
    assert runtime.renew(old) is False
    takeover = runtime.claim("recovery", {"form_goal"}, recover_expired=True)
    assert takeover.mode == "reconcile"
    assert takeover.token != old.token
    assert runtime.record(old, {"schema_version": 1}) == "stale"


def test_heartbeat_cannot_revive_canceled_claim(runtime):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    assert runtime.renew(claim)
    runtime.cancel("alice", task["task_id"], 1)
    assert not runtime.renew(claim)


def test_forged_claim_cannot_record(runtime):
    from dataclasses import replace

    from video_action_agent.runtime import Conflict

    submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    with pytest.raises(Conflict):
        runtime.record(replace(claim, token=uuid4()), {"schema_version": 1})


def test_worker_without_handler_leaves_request_pending(runtime):
    from video_action_agent.worker import Worker

    task = submit(runtime)
    assert Worker(runtime, "w1", {}).run_once() is False
    assert runtime.get("alice", task["task_id"])["work_state"] == "ready"


def test_worker_handler_failure_records_failure_not_success(runtime):
    from video_action_agent.worker import Worker

    def fail(_):
        raise RuntimeError("secret should not be persisted")

    task = submit(runtime)
    assert Worker(runtime, "w1", {"form_goal": fail}).run_once()
    view = runtime.get("alice", task["task_id"])
    assert not view["completed"]
    assert view["last_work_outcome"] == "error"


def build_candidate(**overrides):
    base = {
        "user_goal": "Organize this tutorial into steps",
        "entry_context": None,
        "required_results": [
            {
                "title": "Executable step list",
                "acceptance": "Every step is observable in the delivered list",
                "basis": "user_utterance",
                "evidence": "message: Organize this tutorial",
            }
        ],
        "optional_results": [
            {
                "title": "Source timestamp per step",
                "acceptance": "Each step carries the timestamp it came from",
                "basis": "retractable_inference",
                "evidence": "message: Organize this tutorial (timestamps are my inference)",
            }
        ],
        "constraints": ["No invented steps"],
        "risks": ["Transcript may be partial"],
        "unknowns": [{"item": "Step depth", "resolution": "Follow the user wording"}],
    }
    return {**base, **overrides}


def test_formation_review_requires_input_and_candidate_binding():
    from pydantic import ValidationError

    from video_action_agent.models import FormationReview

    with pytest.raises(ValidationError):
        FormationReview.model_validate({"version": "review-v1", "passed": True})


def build_command(claim, decision="accept", *, passed=True, candidate=None, **overrides):
    from pydantic import ValidationError

    from video_action_agent.models import GoalCandidate
    from video_action_agent.runtime import digest

    candidate = build_candidate() if candidate is None else candidate
    try:
        fingerprint = digest(GoalCandidate.model_validate(candidate).model_dump())
    except ValidationError:
        # Broken candidates must reach the controller so it can reject them itself.
        fingerprint = "0" * 64
    input_fingerprint = digest(claim.request)
    review = {
        "version": "review-v0",
        "passed": passed,
        "input_fingerprint": input_fingerprint,
        "candidate_fingerprint": fingerprint,
    }
    payload = {
        "schema_version": 1,
        "decision": decision,
        "input_fingerprint": input_fingerprint,
        "candidate_fingerprint": fingerprint,
        "candidate": candidate,
        "input_contract_review": {**review, "version": "input-contract-v0"},
        "policy_review": {**review, "version": "policy-v0"},
        "blocker": None,
        "authority": None,
    }
    if decision == "needs_user":
        payload["blocker"] = {
            "question": "Which sections should the step list cover?",
            "waiting_condition": "Task owner names the sections",
            "responsible_party": "task_owner",
            "advanceable": False,
            "exit_path": "The answer arrives as a new explicit input and contract version",
        }
    if decision == "unformable":
        payload["authority"] = {
            "kind": "product_capability",
            "reason": "Step extraction is outside the shipped product capability",
        }
    return {**payload, **overrides}


def test_accept_decision_writes_formal_goal_state_atomically(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    result = runtime.form_decision(claim, "alice", build_command(claim))
    assert result["decision"] == "accept"
    assert result["disposition"] == "recorded"
    view = runtime.get("alice", task["task_id"])
    assert view["contract_state"] == "accepted"
    assert view["completed"] is False
    assert view["work_state"] == "closed"
    assert view["last_work_outcome"] == "ok"
    formation = view["formation"]
    assert formation["decision"] == "accept"
    assert formation["waiting"] is False
    assert formation["blocker"] is None
    assert [(g["title"], g["state"], g["progress"]) for g in formation["required_goals"]] == [
        ("Executable step list", "active", "none")
    ]
    assert [g["basis"] for g in formation["optional_goals"]] == ["retractable_inference"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM goals")).scalar_one() == 2
        assert (
            conn.execute(text("SELECT semantic_fidelity_proven FROM goal_formations")).scalar_one()
            is False
        )
        assert conn.execute(
            text("SELECT kind FROM transitions ORDER BY revision")
        ).scalars().all() == ["admitted", "goal_formation"]


def test_goal_storage_rejects_satisfied_without_complete_progress(runtime, engine):
    from sqlalchemy.exc import IntegrityError

    submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim))
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(text("UPDATE goals SET state='satisfied' WHERE required=true"))


def test_needs_user_keeps_forming_and_records_real_blocker(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    result = runtime.form_decision(claim, "alice", build_command(claim, decision="needs_user"))
    assert result["decision"] == "needs_user"
    view = runtime.get("alice", task["task_id"])
    assert view["contract_state"] == "forming"
    assert view["work_state"] == "closed"
    formation = view["formation"]
    assert formation["decision"] == "needs_user"
    assert formation["waiting"] is True
    assert formation["blocker"]["question"] == "Which sections should the step list cover?"
    assert formation["blocker"]["responsible_party"] == "task_owner"
    assert formation["blocker"]["exit_path"]
    assert formation["blocker"]["state"] == "open"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goals")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM goal_blockers")).scalar_one() == 1


def test_unformable_needs_capability_or_policy_authority(runtime, engine):
    from pydantic import ValidationError

    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    for bad in ("source_unavailable", "model_timeout", "adapter_failed"):
        with pytest.raises(ValidationError):
            runtime.form_decision(
                claim,
                "alice",
                build_command(
                    claim,
                    decision="unformable",
                    authority={"kind": bad, "reason": "the subtitle read failed"},
                ),
            )
    assert (
        runtime.form_decision(claim, "alice", build_command(claim, decision="unformable"))[
            "decision"
        ]
        == "unformable"
    )
    view = runtime.get("alice", task["task_id"])
    assert view["contract_state"] == "unformable"
    assert view["formation"]["decision"] == "unformable"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goals")).scalar_one() == 0


def test_formation_rejects_malformed_commands_and_unpassed_reviews(runtime, engine):
    from pydantic import ValidationError

    from video_action_agent.runtime import Conflict

    submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    blank = {**build_candidate()["required_results"][0], "acceptance": "   "}
    for broken in (
        build_command(claim, candidate=build_candidate(required_results=[])),
        build_command(claim, candidate=build_candidate(required_results=[blank])),
        build_command(claim, decision="needs_user", blocker=None),
        build_command(claim, decision="unformable", authority=None),
        {**build_command(claim), "surprise": 1},
    ):
        with pytest.raises(ValidationError):
            runtime.form_decision(claim, "alice", broken)
    with pytest.raises(Conflict, match="candidate fingerprint"):
        runtime.form_decision(claim, "alice", build_command(claim, candidate_fingerprint="0" * 64))
    with pytest.raises(Conflict, match="input fingerprint"):
        runtime.form_decision(claim, "alice", build_command(claim, input_fingerprint="0" * 64))
    with pytest.raises(Conflict, match="reviews"):
        runtime.form_decision(claim, "alice", build_command(claim, passed=False))
    for review_name in ("input_contract_review", "policy_review"):
        command = build_command(claim, candidate=build_candidate(user_goal="Changed proposal"))
        command[review_name] = build_command(claim)[review_name]
        with pytest.raises(Conflict, match="review fingerprint"):
            runtime.form_decision(claim, "alice", command)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM work_receipts")).scalar_one() == 0


def test_same_lease_repeat_is_idempotent_and_changed_content_conflicts(runtime, engine):
    from video_action_agent.runtime import Conflict

    submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    command = build_command(claim)
    first = runtime.form_decision(claim, "alice", command)
    assert runtime.form_decision(claim, "alice", command) == {
        "decision": "accept",
        "formation_id": first["formation_id"],
        "disposition": "recorded",
        "idempotent": True,
    }
    with pytest.raises(Conflict, match="different content"):
        runtime.form_decision(
            claim, "alice", build_command(claim, candidate=build_candidate(user_goal="Other goal"))
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM work_receipts")).scalar_one() == 1


def test_reopened_work_after_decision_is_inconsistent_not_a_second_success(runtime, engine):
    from video_action_agent.runtime import Conflict

    submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    command = build_command(claim, decision="needs_user")
    runtime.form_decision(claim, "alice", command)
    # A stray re-open of the same work item is how a second lease would appear.
    with engine.begin() as conn:
        conn.execute(
            text("""
            UPDATE work_items SET state='ready',lease_token=NULL,lease_owner=NULL,lease_until=NULL,
                expected_revision=(SELECT revision FROM tasks WHERE task_id=work_items.task_id)
            """)
        )
    second = runtime.claim("w2", {"form_goal"})
    assert second is not None and second.token != claim.token
    with pytest.raises(Conflict, match="inconsistent_decision_state"):
        runtime.form_decision(second, "alice", command)
    with pytest.raises(Conflict, match="different content"):
        runtime.form_decision(
            second,
            "alice",
            build_command(second, decision="needs_user", candidate=build_candidate(user_goal="x")),
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM goal_blockers")).scalar_one() == 1


def test_late_decision_after_cancel_is_stale_receipt_only(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.cancel("alice", task["task_id"], 1)
    assert runtime.form_decision(claim, "alice", build_command(claim)) == {
        "decision": "accept",
        "formation_id": None,
        "disposition": "stale",
    }
    view = runtime.get("alice", task["task_id"])
    assert view["contract_state"] == "canceled"
    assert view["formation"] is None
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM goals")).scalar_one() == 0
        assert conn.execute(text("SELECT disposition FROM work_receipts")).scalars().all() == [
            "stale"
        ]


def test_late_decision_after_revision_is_stale_receipt_only(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.revise("alice", task["task_id"], 1, {**REQUEST, "message": "New goal"})
    assert runtime.form_decision(claim, "alice", build_command(claim))["disposition"] == "stale"
    assert runtime.get("alice", task["task_id"])["formation"] is None
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 0


def test_reconcile_lease_can_neither_record_nor_decide(runtime, engine):
    from video_action_agent.runtime import Conflict

    submit(runtime)
    old = runtime.claim("w1", {"form_goal"})
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    takeover = runtime.claim("rescuer", {"form_goal"}, recover_expired=True)
    assert takeover.mode == "reconcile"
    with pytest.raises(Conflict, match="resolver"):
        runtime.record(takeover, {"schema_version": 1})
    with pytest.raises(Conflict, match="execute lease"):
        runtime.form_decision(takeover, "alice", build_command(takeover))
    assert runtime.form_decision(old, "alice", build_command(old))["disposition"] == "stale"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 0


def test_accepted_version_is_superseded_by_new_input_and_goals_end(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim))
    revised = runtime.revise("alice", task["task_id"], 2, {**REQUEST, "message": "New goal"})
    assert revised["contract_state"] == "forming"
    assert revised["formation"] is None
    assert revised["revision"] == 3
    assert runtime.claim("w2", {"form_goal"}) is not None
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT state FROM task_contracts ORDER BY version")
        ).scalars().all() == ["superseded", "forming"]
        assert conn.execute(text("SELECT state FROM goals")).scalars().all() == [
            "superseded",
            "superseded",
        ]


def test_revised_contract_can_form_its_own_goal(runtime):
    task = submit(runtime)
    revised = runtime.revise(
        "alice", task["task_id"], 1, {**REQUEST, "message": "Create a new step list"}
    )
    assert revised["current_version"] == 2
    claim = runtime.claim("w2", {"form_goal"})
    assert claim.contract_version == 2
    result = runtime.form_decision(claim, "alice", build_command(claim))
    assert result["disposition"] == "recorded"
    view = runtime.get("alice", task["task_id"])
    assert view["contract_state"] == "accepted"
    assert view["formation"]["decision"] == "accept"


def test_replacing_a_blocked_goal_does_not_claim_the_question_was_answered(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim, decision="needs_user"))
    runtime.revise(
        "alice", task["task_id"], 2, {**REQUEST, "message": "Instead, summarize the video"}
    )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT state FROM goal_blockers")).scalar_one() == "canceled"


def test_unformable_version_is_kept_terminal_while_new_input_gets_a_new_version(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim, decision="unformable"))
    revised = runtime.revise("alice", task["task_id"], 2, {**REQUEST, "message": "Now clear"})
    assert revised["contract_state"] == "forming"
    assert revised["current_version"] == 2
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT state FROM task_contracts ORDER BY version")
        ).scalars().all() == ["unformable", "forming"]


def test_cancel_closes_active_goals_and_open_blockers(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim))
    canceled = runtime.cancel("alice", task["task_id"], 2)
    assert canceled["contract_state"] == "canceled"
    assert [g["state"] for g in canceled["formation"]["required_goals"]] == ["canceled"]
    with engine.connect() as conn:
        assert conn.execute(text("SELECT state FROM goals")).scalars().all() == [
            "canceled",
            "canceled",
        ]


def test_cancel_terminal_unformable_is_no_op_without_false_transition(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim, decision="unformable"))
    view = runtime.cancel("alice", task["task_id"], 2)
    assert view["contract_state"] == "unformable"
    assert view["revision"] == 2
    with engine.connect() as conn:
        assert conn.execute(text("SELECT state FROM task_contracts")).scalar_one() == "unformable"
        assert conn.execute(
            text("SELECT kind FROM transitions ORDER BY revision")
        ).scalars().all() == [
            "admitted",
            "goal_formation",
        ]


def test_formation_commit_rolls_back_completely_on_failure(runtime, engine):
    def fault(conn, cursor, statement, params, context, many):
        if "UPDATE tasks SET revision=revision+1" in statement:
            raise RuntimeError("injected formation failure")

    submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    event.listen(engine, "before_cursor_execute", fault)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            runtime.form_decision(claim, "alice", build_command(claim))
    finally:
        event.remove(engine, "before_cursor_execute", fault)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM goals")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM work_receipts")).scalar_one() == 0
        assert conn.execute(text("SELECT state FROM task_contracts")).scalar_one() == "forming"
        assert conn.execute(text("SELECT state FROM work_items")).scalar_one() == "leased"
    assert runtime.form_decision(claim, "alice", build_command(claim))["disposition"] == "recorded"


def test_recovery_resets_undispatched_work_and_fences_the_old_lease(runtime, engine):
    task = submit(runtime)
    old = runtime.claim("w1", {"form_goal"})
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    reconciler = runtime.claim("rescuer", {"form_goal"}, recover_expired=True)
    assert reconciler.mode == "reconcile"
    assert runtime.recover_form_goal(reconciler, "alice") == {
        "action": "reset_to_ready",
        "work_id": str(old.work_id),
        "reset": True,
    }
    assert runtime.recover_form_goal(reconciler, "alice")["action"] == "already_released"
    fresh = runtime.claim("w2", {"form_goal"})
    assert fresh is not None and fresh.mode == "execute"
    assert fresh.work_id == old.work_id
    assert runtime.record(old, {"schema_version": 1}) == "stale"
    view = runtime.get("alice", task["task_id"])
    assert view["formation"] is None
    with engine.connect() as conn:
        assert conn.execute(text("SELECT action FROM form_goal_recoveries")).scalars().all() == [
            "reset_to_ready",
            "already_released",
        ]


def test_recovery_alarms_when_decision_exists_but_work_is_open(runtime, engine):
    submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim, decision="needs_user"))
    # Simulate the broken state the design calls a consistency violation.
    with engine.begin() as conn:
        conn.execute(
            text("""
            UPDATE work_items SET state='ready',lease_token=NULL,lease_owner=NULL,lease_until=NULL,
                expected_revision=(SELECT revision FROM tasks WHERE task_id=work_items.task_id)
            """)
        )
    runtime.claim("w2", {"form_goal"})
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    reconciler = runtime.claim("rescuer", {"form_goal"}, recover_expired=True)
    assert runtime.recover_form_goal(reconciler, "alice")["action"] == "inconsistent_decision_state"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT state FROM work_items")).scalar_one() == "leased"
        assert conn.execute(text("SELECT action FROM form_goal_recoveries")).scalars().all() == [
            "inconsistent_decision_state"
        ]


def test_formation_and_recovery_owner_isolation(runtime, engine):
    from video_action_agent.runtime import Conflict

    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    with pytest.raises(Conflict, match="not authorized"):
        runtime.form_decision(claim, "bob", build_command(claim))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    reconciler = runtime.claim("rescuer", {"form_goal"}, recover_expired=True)
    with pytest.raises(Conflict, match="not authorized"):
        runtime.recover_form_goal(reconciler, "bob")
    view = runtime.get("alice", task["task_id"])
    assert view["formation"] is None
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 0


def test_record_payload_cannot_fake_a_formation(runtime, engine):
    from video_action_agent.runtime import Conflict

    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.record(claim, {"schema_version": 1, "stage": "goal_accepted", "decision": "accept"})
    view = runtime.get("alice", task["task_id"])
    assert view["formation"] is None
    assert view["contract_state"] == "forming"
    assert view["completed"] is False
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM goal_formations")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM goals")).scalar_one() == 0
    # The lease receipt is already used; its content can never be replaced.
    with pytest.raises(Conflict, match="Immutable receipt"):
        runtime.form_decision(claim, "alice", build_command(claim))


def test_decision_key_is_unique_per_contract_version(runtime, engine):
    from sqlalchemy.exc import IntegrityError

    submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim))
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text("""
                INSERT INTO goal_formations
                (formation_id,task_id,contract_version,work_id,lease_token,revision,decision,
                 command,command_hash,input_fingerprint,candidate_fingerprint,
                 input_contract_review,policy_review)
                SELECT :new_id,task_id,contract_version,work_id,lease_token,revision,decision,
                       command,command_hash,input_fingerprint,candidate_fingerprint,
                       input_contract_review,policy_review
                FROM goal_formations
                """),
            {"new_id": uuid4()},
        )


def test_claim_task_scope_selects_only_the_named_task(runtime):
    owner = "local-source-probe-" + uuid4().hex
    first = runtime.submit(owner, "first", REQUEST)
    second = runtime.submit(owner, "second", REQUEST)
    claim = runtime.claim("w1", {"form_goal"}, task_id=second["task_id"])
    assert claim is not None and claim.task_id == second["task_id"]
    other = runtime.claim("w2", {"form_goal"}, task_id=first["task_id"])
    assert other is not None and other.task_id == first["task_id"]
    assert runtime.claim("w3", {"form_goal"}, task_id=second["task_id"]) is None
    # No scope keeps the previous behavior for every caller.
    assert runtime.claim("w4", {"form_goal"}, allowed_owner="nobody") is None


def test_claim_task_scope_never_spills_to_unknown_or_foreign_tasks(runtime):
    task = runtime.submit("alice", "one", REQUEST)
    foreign = runtime.submit("bob", "two", REQUEST)
    assert runtime.claim("w1", {"form_goal"}, task_id=uuid4()) is None
    assert (
        runtime.claim("w2", {"form_goal"}, allowed_owner="alice", task_id=foreign["task_id"])
        is None
    )
    assert runtime.get("alice", task["task_id"])["work_state"] == "ready"
    assert runtime.get("alice", task["task_id"])["revision"] == 1
    with pytest.raises(ValueError):
        runtime.claim("w3", {"form_goal"}, task_id="not-a-uuid")


def insert_work(engine, task, kind, revision, state="ready"):
    """Controlled test insertion only: production never enqueues this work yet."""
    work = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO work_items
            (work_id,task_id,contract_version,expected_revision,run_id,kind,state)
            VALUES (:work,:task,:version,:revision,:run,:kind,:state)
            """),
            {
                "work": work,
                "task": task["task_id"],
                "version": task["current_version"],
                "revision": revision,
                "run": uuid4(),
                "kind": kind,
                "state": state,
            },
        )
    return work


def test_accepted_contract_registers_and_claims_successor_work(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim))
    formed = runtime.get("alice", task["task_id"])
    assert formed["contract_state"] == "accepted"
    assert formed["revision"] == 2
    work = insert_work(engine, task, "read_source", formed["revision"])
    # The closed formation work must not shadow the current successor.
    view = runtime.get("alice", task["task_id"])
    assert view["work_kind"] == "read_source"
    assert view["work_state"] == "ready"
    assert view["work_current"] is True
    assert runtime.claim("w2", {"form_goal"}) is None
    successor = runtime.claim("w2", {"read_source"})
    assert successor is not None
    assert successor.kind == "read_source"
    assert successor.work_id == work
    assert runtime.renew(successor) is True
    leased = runtime.get("alice", task["task_id"])
    assert leased["work_state"] == "leased"
    assert leased["work_current"] is True


def test_forming_contract_never_claims_registered_successor_work(runtime, engine):
    task = submit(runtime)
    insert_work(engine, task, "read_source", 1)
    assert runtime.claim("w1", {"read_source"}) is None
    mixed = runtime.claim("w1", {"form_goal", "read_source"})
    assert mixed is not None and mixed.kind == "form_goal"
    view = runtime.get("alice", task["task_id"])
    assert view["work_kind"] == "form_goal"
    assert view["work_current"] is True


def test_stale_revision_successor_work_is_never_current(runtime, engine):
    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim))
    insert_work(engine, task, "read_source", 1)
    assert runtime.claim("w2", {"read_source"}) is None
    assert runtime.claim("w2", {"form_goal", "read_source"}) is None
    view = runtime.get("alice", task["task_id"])
    assert view["work_kind"] == "read_source"
    assert view["work_state"] == "ready"
    assert view["work_current"] is False


def test_task_view_never_mixes_pre_cancel_contract_with_post_cancel_work(runtime, engine):
    from threading import Event, get_ident

    task = submit(runtime)
    base_read_finished = Event()
    allow_reader_to_continue = Event()
    reader_ident = {}

    def pause_after_task_read(conn, cursor, statement, params, context, many):
        if (
            get_ident() == reader_ident.get("value")
            and "SELECT t.task_id, t.owner_id, t.revision" in statement
        ):
            base_read_finished.set()
            assert allow_reader_to_continue.wait(10)

    def read_task():
        reader_ident["value"] = get_ident()
        return runtime.get("alice", task["task_id"])

    event.listen(engine, "after_cursor_execute", pause_after_task_read)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                result = pool.submit(read_task)
                assert base_read_finished.wait(10)
                runtime.cancel("alice", task["task_id"], 1)
            finally:
                allow_reader_to_continue.set()
            view = result.result(timeout=10)
    finally:
        event.remove(engine, "after_cursor_execute", pause_after_task_read)
    # The read began before cancellation. A single snapshot may return that
    # earlier state, but cannot combine it with the later suppressed work.
    assert view["contract_state"] == "forming"
    assert view["work_state"] == "ready"
    assert view["work_current"] is True


def test_generic_record_cannot_close_successor_work(runtime, engine):
    from video_action_agent.runtime import Conflict

    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim))
    formed = runtime.get("alice", task["task_id"])
    insert_work(engine, task, "read_source", formed["revision"])
    successor = runtime.claim("w2", {"read_source"})
    with pytest.raises(Conflict):
        runtime.record(successor, {"schema_version": 1, "stage": "source_done"})
    after = runtime.get("alice", task["task_id"])
    assert after["work_state"] == "leased"
    assert after["revision"] == formed["revision"]
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM work_receipts WHERE work_id=:work"),
                {"work": successor.work_id},
            ).scalar_one()
            == 0
        )


def test_expired_successor_work_only_yields_a_reconcile_lease(runtime, engine):
    from dataclasses import replace

    from video_action_agent.runtime import Conflict

    task = submit(runtime)
    claim = runtime.claim("w1", {"form_goal"})
    runtime.form_decision(claim, "alice", build_command(claim))
    formed = runtime.get("alice", task["task_id"])
    insert_work(engine, task, "read_source", formed["revision"])
    successor = runtime.claim("w2", {"read_source"})
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    # GOAL-013 changed this fence: read_source may be taken over, but never as an
    # execute lease, and the legacy consumers still cannot touch it.
    takeover = runtime.claim("rescuer", {"read_source"}, recover_expired=True)
    assert takeover is not None and takeover.mode == "reconcile"
    with pytest.raises(Conflict):
        runtime.recover_form_goal(replace(successor, mode="reconcile"), "alice")
    with pytest.raises(Conflict):
        runtime.record(successor, {"schema_version": 1})
    view = runtime.get("alice", task["task_id"])
    # Still the current successor work, but nothing may take it over or close it yet.
    assert view["work_kind"] == "read_source"
    assert view["work_state"] == "leased"
    assert view["work_current"] is True


def accepted_read_source_lease(runtime, engine, actions=("subtitle",), key="scoped"):
    """Accepted scoped task; only legacy/non-product scopes need a test work row."""
    request = {**REQUEST, "local_browser_actions": list(actions)}
    if "subtitle" in actions:
        request["output_kind"] = "method_list"
    task = runtime.submit("alice", key, request)
    claim = runtime.claim("w1", {"form_goal"}, task_id=task["task_id"])
    runtime.form_decision(claim, "alice", build_command(claim))
    formed = runtime.get("alice", task["task_id"])
    if "subtitle" not in actions:
        insert_work(engine, task, "read_source", formed["revision"])
    lease = runtime.claim("w2", {"read_source"}, task_id=task["task_id"])
    assert lease is not None and lease.kind == "read_source"
    return task, lease


def test_accepted_subtitle_scope_creates_claimable_source_work_without_manual_insertion(runtime, engine):
    task = runtime.submit(
        "alice", "product-subtitle",
        {**REQUEST, "output_kind": "method_list", "local_browser_actions": ["subtitle"]},
    )
    formation = runtime.claim("former", {"form_goal"}, task_id=task["task_id"])
    runtime.form_decision(formation, "alice", build_command(formation))

    view = runtime.get("alice", task["task_id"])
    assert (view["contract_state"], view["work_kind"], view["work_state"]) == (
        "accepted", "read_source", "ready"
    )
    source = runtime.claim("source-worker", {"read_source"}, task_id=task["task_id"])
    assert source is not None and source.revision == view["revision"]
    assert runtime.claim("other", {"form_goal"}, task_id=task["task_id"]) is None
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM work_items WHERE task_id=:task AND kind='read_source'"),
            {"task": task["task_id"]},
        ).scalar_one() == 1


def test_browser_scope_alone_does_not_schedule_a_source_read(runtime, engine):
    task = runtime.submit(
        "alice", "scope-only", {**REQUEST, "local_browser_actions": ["subtitle"]}
    )
    formation = runtime.claim("former", {"form_goal"}, task_id=task["task_id"])
    runtime.form_decision(formation, "alice", build_command(formation))
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT count(*) FROM work_items WHERE task_id=:task AND kind='read_source'"),
            {"task": task["task_id"]},
        ).scalar_one() == 0


def test_source_action_candidate_is_precisely_bound_and_read_only(runtime, engine):
    from video_action_agent.runtime import digest

    task, lease = accepted_read_source_lease(runtime, engine)

    def state_counts():
        with engine.connect() as conn:
            return {
                "source_reads": conn.execute(text("SELECT count(*) FROM source_reads")).scalar_one(),
                "goals": conn.execute(text("SELECT count(*) FROM goals")).scalar_one(),
                "receipts": conn.execute(text("SELECT count(*) FROM work_receipts")).scalar_one(),
                "revision": runtime.get("alice", task["task_id"])["revision"],
            }

    before = state_counts()
    candidate = runtime.source_action_candidate(lease, "alice", "subtitle")
    assert candidate.schema_version == 1
    assert candidate.task_id == task["task_id"]
    assert candidate.contract_version == task["current_version"] == 1
    assert candidate.work_id == lease.work_id
    assert candidate.revision == before["revision"]
    assert candidate.action == "subtitle"
    assert (candidate.bvid, candidate.page) == ("BV1gD421u75J", 1)
    assert candidate.entry_scope == ("subtitle",)
    with engine.connect() as conn:
        persisted = conn.execute(
            text("SELECT input FROM task_contracts WHERE task_id=:task AND version=:version"),
            {"task": task["task_id"], "version": task["current_version"]},
        ).scalar_one()
    assert candidate.input_fingerprint == digest(persisted)
    assert state_counts() == before


def test_source_action_candidate_rejects_undeclared_or_mismatched_entry_scope(runtime, engine):
    from video_action_agent.runtime import Conflict

    # A legacy contract without local_browser_actions has an empty entry scope.
    legacy = submit(runtime, key="legacy")
    claim = runtime.claim("w1", {"form_goal"}, task_id=legacy["task_id"])
    runtime.form_decision(claim, "alice", build_command(claim))
    formed = runtime.get("alice", legacy["task_id"])
    insert_work(engine, legacy, "read_source", formed["revision"])
    lease = runtime.claim("w2", {"read_source"}, task_id=legacy["task_id"])
    for action in ("video", "subtitle"):
        with pytest.raises(Conflict):
            runtime.source_action_candidate(lease, "alice", action)
    # A declared ['subtitle'] entry scope never covers 'video'.
    _task, scoped = accepted_read_source_lease(runtime, engine, key="scoped")
    with pytest.raises(Conflict):
        runtime.source_action_candidate(scoped, "alice", "video")


def test_source_action_candidate_rejects_forged_or_expired_lease_and_owner(runtime, engine):
    from dataclasses import replace

    from video_action_agent.runtime import Conflict

    task, lease = accepted_read_source_lease(runtime, engine, key="forgeries")
    forming = submit(runtime, key="forming")
    formation = runtime.claim("w3", {"form_goal"}, task_id=forming["task_id"])
    with pytest.raises(Conflict):
        runtime.source_action_candidate(lease, "bob", "subtitle")
    with pytest.raises(Conflict):
        runtime.source_action_candidate(replace(lease, owner="mallory"), "alice", "subtitle")
    forged_request = replace(
        lease,
        request={**lease.request, "local_browser_actions": ["video", "subtitle"]},
    )
    with pytest.raises(Conflict):
        runtime.source_action_candidate(forged_request, "alice", "video")
    with pytest.raises(Conflict):
        runtime.source_action_candidate(replace(lease, task_id=uuid4()), "alice", "subtitle")
    with pytest.raises(Conflict):
        runtime.source_action_candidate(formation, "alice", "subtitle")
    with pytest.raises(Conflict):
        runtime.source_action_candidate(replace(lease, mode="reconcile"), "alice", "subtitle")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    with pytest.raises(Conflict):
        runtime.source_action_candidate(lease, "alice", "subtitle")
    with pytest.raises(ValueError):
        runtime.source_action_candidate(lease, "", "subtitle")
    with pytest.raises(ValueError):
        runtime.source_action_candidate(lease, "alice", "download")
    assert runtime.get("alice", task["task_id"])["work_state"] == "leased"


def admission_counts(engine):
    with engine.connect() as conn:
        return {
            "proposals": conn.execute(text("SELECT count(*) FROM source_proposals")).scalar_one(),
            "decisions": conn.execute(
                text("SELECT count(*) FROM source_permission_decisions")
            ).scalar_one(),
            "operations": conn.execute(
                text("SELECT count(*) FROM source_reads WHERE permission_decision_id IS NOT NULL")
            ).scalar_one(),
            "commits": conn.execute(text("SELECT count(*) FROM transitions")).scalar_one(),
            "leases": conn.execute(text("SELECT count(*) FROM work_leases")).scalar_one(),
        }


def test_prepare_source_read_creates_the_fully_linked_prepared_admission(runtime, engine):
    from video_action_agent.runtime import digest

    task, lease = accepted_read_source_lease(runtime, engine)
    before = admission_counts(engine)
    base = runtime.get("alice", task["task_id"])
    assert base["revision"] == 2
    admission = runtime.prepare_source_read(lease, "alice", "subtitle")
    assert admission.uniqueness_decision == "new"
    assert (admission.action, admission.bvid, admission.page) == ("subtitle", "BV1gD421u75J", 1)
    assert admission.revision == base["revision"] + 3 == 5
    fresh = admission.claim
    assert fresh.token != lease.token
    assert (fresh.revision, fresh.work_id, fresh.kind, fresh.mode) == (
        admission.revision,
        lease.work_id,
        "read_source",
        "execute",
    )
    with engine.connect() as conn:
        proposal = conn.execute(text("SELECT * FROM source_proposals")).mappings().one()
        decision = conn.execute(text("SELECT * FROM source_permission_decisions")).mappings().one()
        operation = conn.execute(
            text("SELECT * FROM source_reads WHERE permission_decision_id IS NOT NULL")
        ).mappings().one()
        commits = conn.execute(
            text("SELECT revision,kind,object_kind,object_ref,from_state,to_state,reason_ref,"
                 "policy_version,dedup_key FROM transitions WHERE revision > 2 ORDER BY revision")
        ).mappings().all()
        leases = conn.execute(
            text("SELECT token,revision,mode FROM work_leases ORDER BY revision")
        ).mappings().all()
        work = conn.execute(
            text("SELECT * FROM work_items WHERE work_id=:work"), {"work": lease.work_id}
        ).mappings().one()
        persisted = conn.execute(
            text("SELECT input FROM task_contracts WHERE task_id=:task AND version=1"),
            {"task": task["task_id"]},
        ).scalar_one()
        required_goals = conn.execute(
            text("SELECT goal_id FROM goals WHERE required ORDER BY goal_id")
        ).scalars().all()
        material = conn.execute(
            text("SELECT count(*) FROM source_reads WHERE material IS NOT NULL OR raw_payload IS NOT NULL")
        ).scalar_one()
    assert proposal["proposal_id"] == decision["proposal_id"] == admission.proposal_id
    assert proposal["operation_id"] == operation["operation_id"] == admission.operation_id
    assert decision["permission_decision_id"] == operation["permission_decision_id"]
    assert operation["proposal_id"] == proposal["proposal_id"]
    assert operation["permission_id"] == decision["permission_decision_id"]
    assert (proposal["state"], decision["decision"], operation["phase"], operation["outcome"]) == (
        "consumed",
        "auto_allow",
        "prepared",
        "none",
    )
    assert (proposal["action"], proposal["bvid"], proposal["page"], proposal["effect"]) == (
        "subtitle",
        "BV1gD421u75J",
        1,
        "read_only",
    )
    assert proposal["policy_version"] == decision["policy_version"] == "accepted-local-bilibili-read-v1"
    assert proposal["input_fingerprint"] == digest(persisted)
    assert proposal["goal_ref"]["required_goal_ids"] == [str(goal) for goal in required_goals]
    assert decision["connection_state"] == "unverified"
    assert decision["authorization_basis"] == "contract_local_browser_actions"
    assert decision["owner_id"] == "alice"
    assert (decision["bvid"], decision["page"], decision["action"]) == ("BV1gD421u75J", 1, "subtitle")
    assert [commit["revision"] for commit in commits] == [3, 4, 5]
    assert [commit["to_state"] for commit in commits] == ["proposed", "authorized", "consumed"]
    assert [commit["object_kind"] for commit in commits] == ["source_proposal"] * 3
    assert [commit["object_ref"] for commit in commits] == [admission.proposal_id] * 3
    assert all(commit["policy_version"] == "accepted-local-bilibili-read-v1" for commit in commits)
    assert all(commit["reason_ref"] is not None for commit in commits)
    assert work["state"] == "leased"
    assert work["expected_revision"] == admission.revision
    assert work["lease_token"] == fresh.token
    assert sorted(lease_row["revision"] for lease_row in leases) == [1, 2, 5]
    kept = next(lease_row for lease_row in leases if lease_row["token"] == lease.token)
    assert (kept["revision"], kept["mode"]) == (2, "execute")
    assert [lease_row["token"] for lease_row in leases if lease_row["revision"] == 5] == [
        fresh.token
    ]
    assert runtime.renew(lease) is False
    assert material == 0
    assert admission_counts(engine) == {**before, "proposals": 1, "decisions": 1, "operations": 1,
                                        "commits": before["commits"] + 3, "leases": before["leases"] + 1}
    # An identical re-send returns the same identities and the current claim, adding nothing.
    again = runtime.prepare_source_read(fresh, "alice", "subtitle")
    assert again.uniqueness_decision == "reuse"
    assert (again.proposal_id, again.permission_decision_id, again.operation_id) == (
        admission.proposal_id,
        admission.permission_decision_id,
        admission.operation_id,
    )
    assert again.claim.token == fresh.token
    assert admission_counts(engine)["commits"] == before["commits"] + 3


def test_prepare_source_read_refuses_out_of_scope_owner_and_forged_leases(runtime, engine):
    from dataclasses import replace

    from video_action_agent.runtime import Conflict

    empty_task, empty_lease = accepted_read_source_lease(runtime, engine, actions=(), key="empty")
    task, lease = accepted_read_source_lease(runtime, engine, key="scope")
    before = admission_counts(engine)
    revisions = {
        empty_task["task_id"]: runtime.get("alice", empty_task["task_id"])["revision"],
        task["task_id"]: runtime.get("alice", task["task_id"])["revision"],
    }
    for forbidden in (
        (empty_lease, "alice", "subtitle"),
        (lease, "alice", "video"),
        (lease, "bob", "subtitle"),
        (replace(lease, owner="mallory"), "alice", "subtitle"),
        (
            replace(lease, request={**lease.request, "local_browser_actions": ["video", "subtitle"]}),
            "alice",
            "subtitle",
        ),
        (replace(lease, task_id=uuid4()), "alice", "subtitle"),
    ):
        with pytest.raises(Conflict):
            runtime.prepare_source_read(*forbidden)
    with pytest.raises(ValueError):
        runtime.prepare_source_read(lease, "", "subtitle")
    with pytest.raises(ValueError):
        runtime.prepare_source_read(lease, "alice", "download")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    with pytest.raises(Conflict):
        runtime.prepare_source_read(lease, "alice", "subtitle")
    assert admission_counts(engine) == before
    for owner, task_id in (("alice", empty_task["task_id"]), ("alice", task["task_id"])):
        assert runtime.get(owner, task_id)["revision"] == revisions[task_id]
    # Even after a real admission, another action cannot claim a second formal operation.
    _task2, lease2 = accepted_read_source_lease(runtime, engine, actions=("video", "subtitle"), key="second")
    admission2 = runtime.prepare_source_read(lease2, "alice", "video")
    with pytest.raises(Conflict):
        runtime.prepare_source_read(admission2.claim, "alice", "subtitle")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_proposals WHERE work_id=:work"),
                            {"work": lease2.work_id}).scalar_one() == 1


def test_formal_admission_needs_its_causal_chain_and_rolls_back_whole(runtime, engine):
    from sqlalchemy.exc import IntegrityError

    from video_action_agent.runtime import Conflict

    task, lease = accepted_read_source_lease(runtime, engine, key="causal")
    admission = runtime.prepare_source_read(lease, "alice", "subtitle")
    fresh = admission.claim
    # No dispatch path may touch the formal prepared row.
    from video_action_agent.source_runtime import SourceRuntime

    service = SourceRuntime(runtime, "alice", True)
    for touch in (
        lambda: service.admit(fresh, "subtitle"),
        lambda: service.begin(fresh, admission.operation_id),
        lambda: service.finish(fresh, admission.operation_id, b"raw"),
        lambda: service.fail(fresh, admission.operation_id, "adapter_failed"),
    ):
        with pytest.raises(Conflict):
            touch()
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT phase,outcome,material,raw_payload FROM source_reads WHERE operation_id=:op"),
            {"op": admission.operation_id},
        ).mappings().one()
    assert (row["phase"], row["outcome"], row["material"], row["raw_payload"]) == (
        "prepared",
        "none",
        None,
        None,
    )
    # A formal operation cannot relabel its proposal or permission while
    # retaining the same admission reference.
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text("UPDATE source_reads SET proposal_id=:other WHERE operation_id=:op"),
            {"other": uuid4(), "op": admission.operation_id},
        )
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
            text("UPDATE source_reads SET permission_id=:other WHERE operation_id=:op"),
            {"other": uuid4(), "op": admission.operation_id},
        )
    # A formal row without its permission decision cannot exist, and one decision
    # can never be consumed twice.
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
                text("""
                INSERT INTO source_reads
                (operation_id,proposal_id,permission_id,attempt_id,admission_id,task_id,
                 contract_version,revision,work_id,lease_token,action,adapter_version,
                 proposal,permission,phase,outcome,permission_decision_id)
                VALUES (:op,:proposal_id,:permission_id,:attempt,:admission,:task,1,:revision,
                        :work,:token,'subtitle','opencli-bilibili-local-v1',CAST(:proposal AS jsonb),
                        CAST(:permission AS jsonb),'prepared','none',:decision)
                """),
                {
                    "op": uuid4(),
                    "proposal_id": uuid4(),
                    "permission_id": uuid4(),
                    "attempt": uuid4(),
                    "admission": uuid4(),
                    "task": task["task_id"],
                    "revision": admission.revision,
                    "work": lease.work_id,
                    "token": fresh.token,
                    "proposal": "{}",
                    "permission": "{}",
                    "decision": admission.permission_decision_id,
                },
            )
    # A permission decision cannot exist without its proposal.
    with engine.begin() as conn, pytest.raises(IntegrityError):
        conn.execute(
                text("""
                INSERT INTO source_permission_decisions
                (permission_decision_id,proposal_id,operation_id,task_id,contract_version,revision,
                 owner_id,authorization_basis,entry_scope,action,bvid,page,effect,data_purpose,
                 policy_version,decision,connection_state,reason_codes)
                VALUES (:decision,:proposal,:operation,:task,1,:revision,'alice',
                        'contract_local_browser_actions',CAST(:scope AS jsonb),'subtitle',
                        'BV1gD421u75J',1,'read_only','goal_support',
                        'accepted-local-bilibili-read-v1','auto_allow','unverified',
                        CAST(:reasons AS jsonb))
                """),
                {
                    "decision": uuid4(),
                    "proposal": uuid4(),
                    "operation": uuid4(),
                    "task": task["task_id"],
                    "revision": admission.revision,
                    "scope": '["subtitle"]',
                    "reasons": "[]",
                },
            )
    # A commit failure leaves no partial admission behind.
    def fault(conn, cursor, statement, params, context, many):
        if "UPDATE work_items SET lease_token=" in statement:
            raise RuntimeError("injected admission failure")

    rollback_lease = accepted_read_source_lease(runtime, engine, key="rollback")[1]
    event.listen(engine, "before_cursor_execute", fault)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            runtime.prepare_source_read(rollback_lease, "alice", "subtitle")
    finally:
        event.remove(engine, "before_cursor_execute", fault)
    assert admission_counts(engine)["proposals"] == 1


def operation_row(engine, operation_id):
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT * FROM source_reads WHERE operation_id=:op"), {"op": operation_id}
            ).mappings().one()
        )


def attempt_rows(engine, operation_id):
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text("SELECT * FROM source_operation_attempts WHERE operation_id=:op ORDER BY attempt_seq"),
                {"op": operation_id},
            ).mappings().all()
        ]


def expire_read_work(engine):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )


def test_prepared_operation_recovers_under_the_same_operation_id(runtime, engine):
    task, lease = accepted_read_source_lease(runtime, engine)
    admission = runtime.prepare_source_read(lease, "alice", "subtitle")
    opened = attempt_rows(engine, admission.operation_id)
    assert [
        (row["attempt_seq"], row["state"], row["dispatch_admission_obtained"]) for row in opened
    ] == [(1, "prepared", False)]
    before = operation_row(engine, admission.operation_id)
    leases_before = admission_counts(engine)["leases"]
    expire_read_work(engine)
    reconciler = runtime.claim(
        "rescuer", {"read_source"}, recover_expired=True, task_id=task["task_id"]
    )
    assert reconciler is not None and reconciler.mode == "reconcile"
    recovered = runtime.recover_source_read(reconciler, "alice")
    assert recovered.action == "attempt_recovered"
    assert recovered.idempotent is False
    assert recovered.operation_id == admission.operation_id
    assert recovered.attempt_seq == 2
    assert recovered.attempt_id != opened[0]["attempt_id"]
    fresh = recovered.claim
    assert fresh.mode == "execute"
    assert fresh.token not in {lease.token, admission.claim.token, reconciler.token}
    assert fresh.revision == admission.revision + 1
    attempts = attempt_rows(engine, admission.operation_id)
    assert [row["attempt_seq"] for row in attempts] == [1, 2]
    assert (
        attempts[0]["state"],
        attempts[0]["close_reason"],
        attempts[0]["dispatch_admission_obtained"],
    ) == ("closed", "interrupted_before_dispatch", False)
    assert attempts[0]["lease_token"] == admission.claim.token
    assert (attempts[1]["state"], attempts[1]["dispatch_admission_obtained"]) == ("prepared", False)
    assert attempts[1]["lease_token"] == fresh.token
    after = operation_row(engine, admission.operation_id)
    assert after["attempt_id"] == recovered.attempt_id
    assert after["lease_token"] == fresh.token
    for kept in (
        "operation_id",
        "proposal_id",
        "permission_id",
        "permission_decision_id",
        "revision",
        "phase",
        "outcome",
        "dispatched_at",
        "received_at",
        "payload_hash",
    ):
        assert after[kept] == before[kept]
    assert runtime.renew(fresh) is True
    assert runtime.renew(lease) is False
    assert runtime.renew(reconciler) is False
    # the reconcile lease plus the recovered execute lease are the only new rows
    assert admission_counts(engine)["leases"] == leases_before + 2
    # A crash-resend with the same reconcile token returns the same attempt and claim.
    again = runtime.recover_source_read(reconciler, "alice")
    assert again.idempotent is True
    assert (again.operation_id, again.attempt_id, again.attempt_seq) == (
        recovered.operation_id,
        recovered.attempt_id,
        recovered.attempt_seq,
    )
    assert again.claim.token == fresh.token
    assert attempt_rows(engine, admission.operation_id) == attempts
    assert operation_row(engine, admission.operation_id) == after
    assert admission_counts(engine)["leases"] == leases_before + 2
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE source_permission_decisions SET decision='reject' "
                 "WHERE permission_decision_id=:decision"),
            {"decision": admission.permission_decision_id},
        )
    assert runtime.recover_source_read(reconciler, "alice").claim is None
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE source_permission_decisions SET decision='auto_allow' "
                 "WHERE permission_decision_id=:decision"),
            {"decision": admission.permission_decision_id},
        )
    # An idempotent resend must not hand back an execute claim after it expires.
    expire_read_work(engine)
    stale = runtime.recover_source_read(reconciler, "alice")
    assert stale.claim is None


def test_possibly_dispatched_or_contradictory_operations_never_retry(runtime, engine):
    from dataclasses import replace

    from video_action_agent.runtime import Conflict

    def recover(target):
        expire_read_work(engine)
        reconciler = runtime.claim(
            "rescuer", {"read_source"}, recover_expired=True, task_id=target["task_id"]
        )
        assert reconciler is not None
        return reconciler

    cases = {}
    dispatched_task, dispatched_lease = accepted_read_source_lease(
        runtime, engine, key="dispatched"
    )
    dispatched = runtime.prepare_source_read(dispatched_lease, "alice", "subtitle")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE source_reads SET dispatched_at=clock_timestamp() WHERE operation_id=:op"),
            {"op": dispatched.operation_id},
        )
    cases["dispatch may have started"] = (dispatched_task, dispatched)
    admitted_task, admitted_lease = accepted_read_source_lease(runtime, engine, key="admitted")
    admitted = runtime.prepare_source_read(admitted_lease, "alice", "subtitle")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE source_operation_attempts SET dispatch_admission_obtained=true WHERE operation_id=:op"),
            {"op": admitted.operation_id},
        )
    cases["the attempt once held dispatch admission"] = (admitted_task, admitted)
    unknown_task, unknown_lease = accepted_read_source_lease(runtime, engine, key="unknown")
    unknown = runtime.prepare_source_read(unknown_lease, "alice", "subtitle")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE source_reads SET phase='closed',outcome='outcome_unknown' WHERE operation_id=:op"),
            {"op": unknown.operation_id},
        )
    cases["the operation already closed with unknown outcome"] = (unknown_task, unknown)
    mismatch_task, mismatch_lease = accepted_read_source_lease(
        runtime, engine, key="mismatched-attempt-head"
    )
    mismatch = runtime.prepare_source_read(mismatch_lease, "alice", "subtitle")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE source_reads SET lease_token=:old WHERE operation_id=:op"),
            {"old": mismatch_lease.token, "op": mismatch.operation_id},
        )
    cases["operation projection disagrees with its attempt head"] = (mismatch_task, mismatch)
    revoked_task, revoked_lease = accepted_read_source_lease(
        runtime, engine, key="rejected-permission"
    )
    revoked = runtime.prepare_source_read(revoked_lease, "alice", "subtitle")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE source_permission_decisions SET decision='reject' "
                 "WHERE permission_decision_id=:decision"),
            {"decision": revoked.permission_decision_id},
        )
    cases["the precise permission is no longer allowed"] = (revoked_task, revoked)
    for label, (target, admission) in cases.items():
        frozen = (
            attempt_rows(engine, admission.operation_id),
            operation_row(engine, admission.operation_id),
            runtime.get("alice", target["task_id"])["revision"],
        )
        result = runtime.recover_source_read(recover(target), "alice")
        assert result.action == "requires_source_reconciliation", label
        assert result.claim is None, label
        assert (
            attempt_rows(engine, admission.operation_id),
            operation_row(engine, admission.operation_id),
            runtime.get("alice", target["task_id"])["revision"],
        ) == frozen, label
    # Wrong owner, wrong contract version and a canceled task all write nothing.
    fenced_task, fenced_lease = accepted_read_source_lease(runtime, engine, key="fenced")
    fenced = runtime.prepare_source_read(fenced_lease, "alice", "subtitle")
    reconciler = recover(fenced_task)
    with pytest.raises(Conflict):
        runtime.recover_source_read(reconciler, "bob")
    with pytest.raises(Conflict):
        runtime.recover_source_read(replace(reconciler, contract_version=99), "alice")
    runtime.cancel("alice", fenced_task["task_id"], runtime.get("alice", fenced_task["task_id"])["revision"])
    assert runtime.recover_source_read(reconciler, "alice").action == "version_no_longer_current"
    assert [row["attempt_seq"] for row in attempt_rows(engine, fenced.operation_id)] == [1]


def test_only_work_without_any_source_operation_resets_to_ready(runtime, engine):
    task, _lease = accepted_read_source_lease(runtime, engine, key="no-operation")
    expire_read_work(engine)
    reconciler = runtime.claim(
        "rescuer", {"read_source"}, recover_expired=True, task_id=task["task_id"]
    )
    result = runtime.recover_source_read(reconciler, "alice")
    assert result.action == "reset_to_ready"
    assert (result.operation_id, result.attempt_id, result.claim) == (None, None, None)
    assert runtime.get("alice", task["task_id"])["work_state"] == "ready"
    assert runtime.claim("w2", {"read_source"}, task_id=task["task_id"]) is not None
    # A registered diagnostic operation alone is still "an operation": no reset, no retry.
    second_task, second_lease = accepted_read_source_lease(runtime, engine, key="diagnostic-row")
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO source_reads
            (operation_id,proposal_id,permission_id,attempt_id,admission_id,task_id,
             contract_version,revision,work_id,lease_token,action,adapter_version,
             proposal,permission,phase,outcome)
            VALUES (:op,:proposal_id,:permission_id,:attempt,:admission,:task,1,:revision,
                    :work,:token,'subtitle','opencli-bilibili-local-v1',CAST(:proposal AS jsonb),
                    CAST(:permission AS jsonb),'prepared','none')
            """),
            {
                "op": uuid4(),
                "proposal_id": uuid4(),
                "permission_id": uuid4(),
                "attempt": uuid4(),
                "admission": uuid4(),
                "task": second_task["task_id"],
                "revision": second_lease.revision,
                "work": second_lease.work_id,
                "token": second_lease.token,
                "proposal": "{}",
                "permission": "{}",
            },
        )
    expire_read_work(engine)
    second = runtime.claim(
        "rescuer-2", {"read_source"}, recover_expired=True, task_id=second_task["task_id"]
    )
    assert runtime.recover_source_read(second, "alice").action == "requires_source_reconciliation"
    assert runtime.get("alice", second_task["task_id"])["work_state"] == "leased"
