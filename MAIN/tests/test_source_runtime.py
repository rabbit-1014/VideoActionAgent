import importlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text

from video_action_agent.runtime import Conflict


def api():
    assert importlib.util.find_spec("video_action_agent.source_runtime"), "source runtime missing"
    return importlib.import_module("video_action_agent.source_runtime")


def setup(runtime, owner="alice"):
    task = runtime.submit(owner, "source", {"message": "Read this video", "source": "BV1gD421u75J"})
    claim = runtime.claim("source-worker", {"form_goal"})
    service = api().SourceRuntime(runtime, allowed_owner="alice", browser_authorized=True)
    return task, claim, service


def test_source_dispatch_is_once_and_survives_new_service(runtime, engine):
    _, claim, service = setup(runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        ops = list(pool.map(lambda _: service.admit(claim, "video"), range(2)))
    assert ops[0] == ops[1]
    assert service.begin(claim, ops[0]) is not None
    restored = api().SourceRuntime(runtime, allowed_owner="alice", browser_authorized=True)
    assert restored.begin(claim, ops[0]) is None
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM source_reads")).mappings().one()
    assert row["phase"] == "dispatching"
    assert row["permission"]["source"]["bvid"] == "BV1gD421u75J"
    assert row["proposal"]["purpose"] == "inspect_submitted_source"


def test_owner_and_local_browser_permission_are_required(runtime):
    _, claim, _ = setup(runtime, owner="bob")
    with pytest.raises(Conflict):
        api().SourceRuntime(runtime, "alice", True).admit(claim, "video")
    with pytest.raises(Conflict):
        api().SourceRuntime(runtime, "bob", False).admit(claim, "video")


def test_cancel_before_dispatch_blocks_external_call(runtime):
    task, claim, service = setup(runtime)
    op = service.admit(claim, "video")
    runtime.cancel("alice", task["task_id"], 1)
    with pytest.raises(Conflict):
        service.begin(claim, op)


def test_canceled_late_source_is_history_not_current(runtime):
    task, claim, service = setup(runtime)
    op = service.admit(claim, "video")
    service.begin(claim, op)
    runtime.cancel("alice", task["task_id"], 1)
    disposition = service.finish(
        claim,
        op,
        b'[{"field":"bvid","value":"BV1gD421u75J"},{"field":"title","value":"A tutorial"}]',
    )
    assert disposition == "stale"
    assert service.current("alice", task["task_id"]) == []
    assert not runtime.get("alice", task["task_id"])["completed"]


def test_receipt_is_immutable_and_metadata_is_not_content(runtime, engine):
    task, claim, service = setup(runtime)
    op = service.admit(claim, "video")
    service.begin(claim, op)
    raw = b'[{"field":"bvid","value":"BV1gD421u75J"},{"field":"title","value":"A"}]'
    assert service.finish(claim, op, raw) == "recorded"
    assert service.finish(claim, op, raw) == "recorded"
    with pytest.raises(Conflict):
        service.finish(claim, op, raw + b" ")
    current = service.current("alice", task["task_id"])
    assert current[0]["material"]["kind"] == "metadata"
    assert current[0]["material"]["content_available"] is False
    assert service.current("bob", task["task_id"]) == []


@pytest.mark.parametrize(
    "code",
    ["adapter_failed", "subtitle_not_returned", "browser_connection_failed", "source_auth_required"],
)
def test_failure_is_persisted_without_error_text_or_old_fixture(runtime, engine, code):
    task, claim, service = setup(runtime)
    op = service.admit(claim, "subtitle")
    service.begin(claim, op)
    service.fail(claim, op, code)
    assert service.current("alice", task["task_id"]) == []
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM source_reads")).mappings().one()
    assert row["outcome"] == "failed"
    assert row["error_code"] == code
    assert row["raw_payload"] is None


def test_expired_claim_cannot_dispatch(runtime, engine):
    _, claim, service = setup(runtime)
    op = service.admit(claim, "video")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )
    with pytest.raises(Conflict):
        service.begin(claim, op)


def test_changed_input_cannot_receive_old_material(runtime):
    task, claim, service = setup(runtime)
    op = service.admit(claim, "subtitle")
    service.begin(claim, op)
    runtime.revise(
        "alice", task["task_id"], 1, {"message": "Another task", "source": "BV1gD421u75J"}
    )
    assert service.finish(claim, op, b'[{"from":0,"to":1,"content":"old"}]') == "stale"
    assert service.current("alice", task["task_id"]) == []


def test_source_worker_does_not_claim_other_owners_task(runtime):
    runtime.submit("bob", "b", {"message": "B", "source": "BV1gD421u75J"})
    assert runtime.claim("source-worker", {"form_goal"}, allowed_owner="alice") is None


def test_source_runner_records_material_without_goal_completion(runtime):
    assert importlib.util.find_spec("video_action_agent.source_worker"), "source worker missing"
    run = importlib.import_module("video_action_agent.source_worker")
    owner = "local-source-probe-" + uuid4().hex
    task = runtime.submit(owner, "a", {"message": "Inspect", "source": "BV1gD421u75J"})

    class Adapter:
        def fetch(self, action, ref):
            assert ref.bvid == "BV1gD421u75J"
            return b'[{"field":"bvid","value":"BV1gD421u75J"},{"field":"title","value":"A"}]'

    service = api().SourceRuntime(runtime, owner, True)
    result = run.run_once(service, Adapter(), "inspector", task["task_id"], actions=("video",))
    assert result["reads"][0]["outcome"] == "succeeded"
    assert runtime.get(owner, task["task_id"])["contract_state"] == "forming"
    assert runtime.get(owner, task["task_id"])["completed"] is False
    assert service.current(owner, task["task_id"])[0]["material"]["kind"] == "metadata"


def test_receipt_write_rolls_back_without_losing_dispatch_state(runtime, engine):
    from sqlalchemy import event

    _, claim, service = setup(runtime)
    op = service.admit(claim, "video")
    service.begin(claim, op)

    def fault(conn, cursor, statement, params, context, many):
        if "received_at=clock_timestamp()" in statement:
            raise RuntimeError("injected receipt failure")

    event.listen(engine, "before_cursor_execute", fault)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            service.finish(
                claim,
                op,
                b'[{"field":"bvid","value":"BV1gD421u75J"},{"field":"title","value":"A"}]',
            )
    finally:
        event.remove(engine, "before_cursor_execute", fault)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT phase,raw_payload FROM source_reads")).one()
    assert row.phase == "dispatching"
    assert row.raw_payload is None
    assert service.begin(claim, op) is None


def test_runner_timeout_stays_unknown_in_both_receipts(runtime, engine):
    from video_action_agent.source_access import SourceAccessError
    from video_action_agent.source_worker import run_once

    owner = "local-source-probe-" + uuid4().hex
    task = runtime.submit(owner, "timeout", {"message": "Read", "source": "BV1gD421u75J"})

    class TimeoutAdapter:
        def fetch(self, action, ref):
            raise SourceAccessError("adapter_timeout")

    service = api().SourceRuntime(runtime, owner, True)
    result = run_once(service, TimeoutAdapter(), "w", task["task_id"], actions=("video",))
    assert result["reads"][0]["outcome"] == "outcome_unknown"
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT outcome FROM source_reads")).scalar_one() == "outcome_unknown"
        )


@pytest.mark.parametrize("raw", [b'[{"field":"bvid","value":"BV1111111111"}]', b"{broken"])
def test_rejected_reply_is_retained_but_never_exposed_as_material(runtime, engine, raw):
    from hashlib import sha256

    from video_action_agent.source_worker import run_once

    owner = "local-source-probe-" + uuid4().hex
    task = runtime.submit(owner, "rejected", {"message": "Read", "source": "BV1gD421u75J"})

    class BadReply:
        def fetch(self, action, ref):
            return raw

    service = api().SourceRuntime(runtime, owner, True)
    run_once(service, BadReply(), "w", task["task_id"], actions=("video",))
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM source_reads")).mappings().one()
    assert bytes(row["raw_payload"] or b"") == raw
    assert row["payload_hash"] == sha256(raw).hexdigest()
    assert row["outcome"] == "failed"
    assert row["material"] is None
    assert service.current(owner, task["task_id"]) == []


def test_recovery_requires_source_reconciliation_and_never_retries(runtime, engine):
    task, claim, service = setup(runtime)
    operation = service.admit(claim, "video")
    assert service.begin(claim, operation) is not None
    expire_work(engine)
    reconciler = runtime.claim("rescuer", {"form_goal"}, recover_expired=True)
    assert reconciler.mode == "reconcile"
    assert runtime.recover_form_goal(reconciler, "alice") == {
        "action": "requires_source_reconciliation",
        "work_id": str(claim.work_id),
        "reset": False,
    }
    row = read_operation(engine, operation)
    assert row["phase"] == "dispatching"
    assert row["retired_pre_dispatch"] is False
    view = runtime.get("alice", task["task_id"])
    assert view["work_state"] == "leased"
    assert view["formation"] is None
    assert runtime.claim("w2", {"form_goal"}) is None
    with engine.connect() as conn:
        assert conn.execute(text("SELECT action FROM form_goal_recoveries")).scalars().all() == [
            "requires_source_reconciliation"
        ]


def expire_work(engine):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'")
        )


def read_operation(engine, operation_id):
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text("SELECT * FROM source_reads WHERE operation_id=:op"), {"op": operation_id}
            ).mappings().one()
        )


def take_over(runtime, worker="rescuer"):
    claim = runtime.claim(worker, {"form_goal"}, recover_expired=True)
    assert claim is not None and claim.mode == "reconcile"
    return claim


def test_orphan_prepared_operation_is_retired_as_failed_history(runtime, engine):
    task, claim, service = setup(runtime)
    orphan = service.admit(claim, "video")
    expire_work(engine)
    result = runtime.recover_form_goal(take_over(runtime), "alice")
    assert result["action"] == "reset_to_ready"
    assert result["reset"] is True
    row = read_operation(engine, orphan)
    assert row["retired_pre_dispatch"] is True
    assert (row["phase"], row["outcome"], row["disposition"], row["error_code"]) == (
        "closed",
        "failed",
        "stale",
        "interrupted_before_dispatch",
    )
    assert row["raw_payload"] is None
    assert row["material"] is None
    assert row["payload_hash"] is None
    assert runtime.get("alice", task["task_id"])["work_state"] == "ready"
    with engine.connect() as conn:
        audit = conn.execute(
            text("SELECT detail FROM form_goal_recoveries WHERE action='reset_to_ready'")
        ).mappings().one()
    assert audit["detail"]["retired_operations"] == [str(orphan)]


def test_new_execute_lease_can_re_register_the_same_action_after_recovery(runtime, engine):
    _task, claim, service = setup(runtime)
    orphan = service.admit(claim, "video")
    expire_work(engine)
    runtime.recover_form_goal(take_over(runtime), "alice")
    fresh = runtime.claim("w2", {"form_goal"})
    assert fresh is not None
    assert fresh.mode == "execute"
    assert fresh.token != claim.token
    replacement = service.admit(fresh, "video")
    assert replacement != orphan
    first = read_operation(engine, orphan)
    second = read_operation(engine, replacement)
    assert first["lease_token"] == claim.token
    assert first["retired_pre_dispatch"] is True
    assert second["lease_token"] == fresh.token
    assert second["retired_pre_dispatch"] is False
    assert second["phase"] == "prepared"
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM source_reads WHERE work_id=:work"),
                {"work": claim.work_id},
            ).scalar_one()
            == 2
        )


def test_two_consecutive_pre_dispatch_crashes_each_recover_safely(runtime, engine):
    _task, claim, service = setup(runtime)
    retired = [service.admit(claim, "video")]
    for round_index in range(2):
        expire_work(engine)
        recovered = runtime.recover_form_goal(take_over(runtime, f"rescuer-{round_index}"), "alice")
        assert recovered["action"] == "reset_to_ready"
        fresh = runtime.claim(f"worker-{round_index}", {"form_goal"})
        assert fresh is not None
        if round_index == 0:
            retired.append(service.admit(fresh, "video"))
    for operation_id in retired:
        assert read_operation(engine, operation_id)["retired_pre_dispatch"] is True
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM form_goal_recoveries WHERE action='reset_to_ready'")
            ).scalar_one()
            == 2
        )
        assert (
            conn.execute(text("SELECT count(*) FROM source_reads WHERE work_id=:work"),
                         {"work": claim.work_id}).scalar_one()
            == 2
        )


def test_retired_operation_rejects_late_old_lease_writes(runtime, engine):
    _task, claim, service = setup(runtime)
    orphan = service.admit(claim, "video")
    expire_work(engine)
    runtime.recover_form_goal(take_over(runtime), "alice")
    before = read_operation(engine, orphan)
    for late in (
        lambda: service.begin(claim, orphan),
        lambda: service.finish(claim, orphan, b"[]"),
        lambda: service.fail(claim, orphan, "adapter_failed"),
    ):
        with pytest.raises(Conflict):
            late()
    assert read_operation(engine, orphan) == before


def test_closed_source_receipt_still_blocks_recovery(runtime, engine):
    task, claim, service = setup(runtime)
    operation = service.admit(claim, "video")
    assert service.begin(claim, operation) is not None
    assert (
        service.finish(
            claim,
            operation,
            b'[{"field":"bvid","value":"BV1gD421u75J"},{"field":"title","value":"A"}]',
        )
        == "recorded"
    )
    expire_work(engine)
    assert runtime.recover_form_goal(take_over(runtime), "alice")["action"] == (
        "requires_source_reconciliation"
    )
    row = read_operation(engine, operation)
    assert row["retired_pre_dispatch"] is False
    assert row["outcome"] == "succeeded"
    assert runtime.get("alice", task["task_id"])["work_state"] == "leased"


def test_contradictory_operation_state_still_blocks_recovery(runtime, engine):
    _task, claim, service = setup(runtime)
    operation = service.admit(claim, "video")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE source_reads SET dispatched_at=clock_timestamp() WHERE operation_id=:op"),
            {"op": operation},
        )
    expire_work(engine)
    result = runtime.recover_form_goal(take_over(runtime), "alice")
    assert result["action"] == "requires_source_reconciliation"
    assert result["reset"] is False
    row = read_operation(engine, operation)
    assert row["retired_pre_dispatch"] is False
    assert row["phase"] == "prepared"
    assert runtime.claim("w2", {"form_goal"}) is None


def test_competing_recoverers_never_double_write(runtime, engine):
    from concurrent.futures import ThreadPoolExecutor

    task, claim, service = setup(runtime)
    orphan = service.admit(claim, "video")
    expire_work(engine)
    reconciler = take_over(runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.recover_form_goal(reconciler, "alice"), range(2)))
    assert sorted(result["action"] for result in results) == [
        "already_released",
        "reset_to_ready",
    ]
    assert sum(result["reset"] for result in results) == 1
    assert read_operation(engine, orphan)["retired_pre_dispatch"] is True
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM form_goal_recoveries WHERE action='reset_to_ready'")
            ).scalar_one()
            == 1
        )
        assert runtime.get("alice", task["task_id"])["work_state"] == "ready"


def test_recovery_guards_reject_wrong_owner_task_and_version(runtime, engine):
    from dataclasses import replace

    task, claim, service = setup(runtime)
    orphan = service.admit(claim, "video")
    expire_work(engine)
    reconciler = take_over(runtime)
    for mismatched in (
        (reconciler, "bob"),
        (replace(reconciler, task_id=uuid4()), "alice"),
        (replace(reconciler, contract_version=99), "alice"),
    ):
        with pytest.raises(Conflict):
            runtime.recover_form_goal(*mismatched)
    row = read_operation(engine, orphan)
    assert row["retired_pre_dispatch"] is False
    assert row["phase"] == "prepared"
    assert runtime.get("alice", task["task_id"])["work_state"] == "leased"


def test_recovery_after_cancel_touches_no_current_state(runtime, engine):
    task, claim, service = setup(runtime)
    orphan = service.admit(claim, "video")
    expire_work(engine)
    reconciler = take_over(runtime)
    runtime.cancel("alice", task["task_id"], 1)
    assert runtime.recover_form_goal(reconciler, "alice")["action"] == "version_no_longer_current"
    row = read_operation(engine, orphan)
    assert row["retired_pre_dispatch"] is False
    assert row["phase"] == "prepared"
    assert runtime.get("alice", task["task_id"])["contract_state"] == "canceled"


def runner():
    assert importlib.util.find_spec("video_action_agent.source_worker"), "source worker missing"
    return importlib.import_module("video_action_agent.source_worker")


class RecordingAdapter:
    def __init__(self, payload=b'[{"field":"bvid","value":"BV1gD421u75J"},{"field":"title","value":"A"}]'):
        self.calls = []
        self.payload = payload

    def fetch(self, action, ref):
        self.calls.append(action)
        return self.payload


def probe(runtime, key, message="Inspect this video"):
    return runtime.submit(
        "local-source-probe-" + uuid4().hex, key, {"message": message, "source": "BV1gD421u75J"}
    )


def test_probe_runner_requires_an_explicit_target_before_any_browser_call(runtime, engine):
    task = probe(runtime, "untargeted")
    adapter = RecordingAdapter()
    service = api().SourceRuntime(runtime, task["owner_id"], True)
    result = runner().run_once(service, adapter, "probe")
    assert result["processed"] is False
    assert result["reason"] == "target_task_required"
    assert adapter.calls == []
    view = runtime.get(task["owner_id"], task["task_id"])
    assert view["work_state"] == "ready"
    assert view["revision"] == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_reads")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM work_receipts")).scalar_one() == 0


def test_probe_runner_only_consumes_the_named_target_task(runtime, engine):
    owner = "local-source-probe-" + uuid4().hex
    first = runtime.submit(owner, "first", {"message": "Earlier", "source": "BV1gD421u75J"})
    second = runtime.submit(owner, "second", {"message": "Later", "source": "BV1gD421u75J"})
    adapter = RecordingAdapter()
    service = api().SourceRuntime(runtime, owner, True)
    result = runner().run_once(service, adapter, "probe", second["task_id"], actions=("video",))
    assert result["processed"] is True
    assert result["task_id"] == str(second["task_id"])
    assert result["goal_accepted"] is False
    assert result["product_completed"] is False
    assert adapter.calls == ["video"]
    untouched = runtime.get(owner, first["task_id"])
    assert untouched["work_state"] == "ready"
    assert untouched["revision"] == 1
    closed = runtime.get(owner, second["task_id"])
    assert closed["work_state"] == "closed"
    assert closed["revision"] == 2
    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT task_id FROM source_reads")).mappings().one()["task_id"]
            == second["task_id"]
        )


def test_probe_runner_rejects_unknown_and_foreign_targets_without_touching_work(runtime, engine):
    mine = probe(runtime, "mine")
    foreign = runtime.submit("alice", "foreign", {"message": "Real", "source": "BV1gD421u75J"})
    adapter = RecordingAdapter()
    service = api().SourceRuntime(runtime, mine["owner_id"], True)
    for target in (uuid4(), foreign["task_id"]):
        result = runner().run_once(service, adapter, "probe", target, actions=("video",))
        assert result["processed"] is False
        assert result["reason"] == "target_task_not_claimable"
    assert adapter.calls == []
    for owner, task in ((mine["owner_id"], mine), ("alice", foreign)):
        view = runtime.get(owner, task["task_id"])
        assert view["work_state"] == "ready"
        assert view["revision"] == 1
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM work_receipts")).scalar_one() == 0


def test_probe_runner_rejects_non_probe_owners_before_claiming(runtime, engine):
    task = runtime.submit("alice", "real", {"message": "Real", "source": "BV1gD421u75J"})
    adapter = RecordingAdapter()
    service = api().SourceRuntime(runtime, "alice", True)
    with pytest.raises(Conflict):
        runner().run_once(service, adapter, "probe", task["task_id"], actions=("video",))
    assert adapter.calls == []
    view = runtime.get("alice", task["task_id"])
    assert view["work_state"] == "ready"
    assert view["revision"] == 1


def test_probe_cli_requires_probe_owner_and_explicit_task_id(monkeypatch):
    import sys

    entry = str(uuid4())
    for argv in (
        [
            "source_worker",
            "--owner",
            "alice",
            "--task-id",
            str(uuid4()),
            "--allow-local-browser",
            "--opencli-entry",
            entry,
        ],
        [
            "source_worker",
            "--owner",
            "local-source-probe-no-task",
            "--allow-local-browser",
            "--opencli-entry",
            entry,
        ],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        with pytest.raises(SystemExit) as exit_info:
            runner().main()
        assert exit_info.value.code == 2
