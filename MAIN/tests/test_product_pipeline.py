"""One offline end-to-end continuation proves every stage is actually wired."""

import pytest
from sqlalchemy import text
from test_artifact_runtime import SmallDraftProvider
from test_source_exit import PROOF, RAW_SUBTITLES, prepared

from video_action_agent.deepseek_provider import ProviderError
from video_action_agent.product_pipeline import ProductPipeline
from video_action_agent.runtime import NotFound
from video_action_agent.source_access import SourceAccessError


class FakeAdapter:
    def __init__(self):
        self.calls = 0

    def fetch(self, action, ref):
        assert (action, ref.bvid, ref.page) == ("subtitle", "BV1gD421u75J", 1)
        self.calls += 1
        return RAW_SUBTITLES.encode()


class FakeProbe:
    def observe(self):
        return PROOF


def test_explicit_method_list_runs_from_formation_to_visible_candidate(runtime):
    task = runtime.submit(
        "alice", "product-run", {
            "message": "整理这个视频的字幕制作方法、步骤和检查点",
            "source": "BV1gD421u75J", "output_kind": "method_list",
            "local_browser_actions": ["subtitle"],
        },
    )
    adapter = FakeAdapter()
    pipeline = ProductPipeline(runtime, "alice", adapter, FakeProbe(), SmallDraftProvider())
    assert pipeline.run_once(task["task_id"]) == {"stage": "formation", "decision": "accept"}
    assert pipeline.run_once(task["task_id"]) == {
        "stage": "source", "state": "recorded", "segments": 2
    }
    last = pipeline.run_once(task["task_id"])
    assert last["stage"] == "artifact" and last["state"] == "unreviewed_candidate"
    assert adapter.calls == 1
    assert runtime.get_artifact("alice", task["task_id"])["draft"]["user_request"] == (
        "整理这个视频的字幕制作方法、步骤和检查点"
    )
    assert runtime.get("alice", task["task_id"])["completed"] is False


def test_failed_model_attempt_releases_same_work_for_explicit_retry(runtime, engine):
    task = runtime.submit(
        "alice", "product-model-retry", {
            "message": "整理这个视频的字幕制作方法", "source": "BV1gD421u75J",
            "output_kind": "method_list", "local_browser_actions": ["subtitle"],
        },
    )
    adapter = FakeAdapter()

    class FailingProvider:
        model_id = "test-failing-provider"

        def complete(self, _system, _user):
            raise ProviderError("request_failed")

    failed = ProductPipeline(runtime, "alice", adapter, FakeProbe(), FailingProvider())
    assert failed.run_once(task["task_id"])["decision"] == "accept"
    assert failed.run_once(task["task_id"])["state"] == "recorded"
    revision = runtime.get("alice", task["task_id"])["revision"]
    assert failed.run_once(task["task_id"]) == {
        "stage": "artifact", "state": "retryable_failure", "code": "request_failed"
    }
    assert runtime.get("alice", task["task_id"])["revision"] == revision
    with pytest.raises(NotFound):
        runtime.get_artifact("alice", task["task_id"])
    healthy = ProductPipeline(runtime, "alice", adapter, FakeProbe(), SmallDraftProvider())
    assert healthy.run_once(task["task_id"])["state"] == "unreviewed_candidate"
    assert adapter.calls == 1


def test_pre_dispatch_source_crash_resumes_same_operation_in_pipeline(runtime, engine):
    task, admission, _attempt = prepared(runtime, engine, key="pipeline-recovery")
    with engine.begin() as conn:
        conn.execute(
            text("""
            UPDATE work_items SET lease_until=clock_timestamp()-interval '1 second'
            WHERE work_id=:work
            """),
            {"work": admission.claim.work_id},
        )
    adapter = FakeAdapter()
    pipeline = ProductPipeline(runtime, "alice", adapter, FakeProbe(), SmallDraftProvider())
    assert pipeline.run_once(task["task_id"]) == {
        "stage": "source", "state": "recorded", "segments": 2
    }
    assert adapter.calls == 1
    with engine.connect() as conn:
        operations = conn.execute(
            text("SELECT count(*) FROM source_reads WHERE work_id=:work"),
            {"work": admission.claim.work_id},
        ).scalar_one()
        attempts = conn.execute(
            text("SELECT count(*) FROM source_operation_attempts WHERE operation_id=:op"),
            {"op": admission.operation_id},
        ).scalar_one()
    assert (operations, attempts) == (1, 2)


@pytest.mark.parametrize(
    "code,outcome",
    [("subtitle_not_returned", "failed"), ("browser_connection_failed", "failed"),
     ("adapter_failed", "outcome_unknown")],
)
def test_source_adapter_error_closes_work_without_material_or_automatic_retry(
    runtime, engine, code, outcome
):
    task = runtime.submit(
        "alice", f"product-source-error-{code}", {
            "message": "整理这个视频的方法", "source": "BV1gD421u75J",
            "output_kind": "method_list", "local_browser_actions": ["subtitle"],
        },
    )

    class FailingAdapter:
        calls = 0

        def fetch(self, action, ref):
            self.calls += 1
            raise SourceAccessError(code)

    adapter = FailingAdapter()
    pipeline = ProductPipeline(runtime, "alice", adapter, FakeProbe(), SmallDraftProvider())
    assert pipeline.run_once(task["task_id"])["decision"] == "accept"
    assert pipeline.run_once(task["task_id"]) == {
        "stage": "source", "state": outcome, "code": code,
    }
    assert pipeline.run_once(task["task_id"])["stage"] == "idle"
    assert adapter.calls == 1
    assert runtime.read_formal_materials("alice", task["task_id"]) == []
    assert runtime.get("alice", task["task_id"])["completed"] is False
    with engine.connect() as conn:
        operation = conn.execute(
            text("SELECT phase,outcome,error_code FROM source_reads WHERE task_id=:task"),
            {"task": task["task_id"]},
        ).one()
        active = conn.execute(
            text("SELECT count(*) FROM goals WHERE task_id=:task AND required AND state='active'"),
            {"task": task["task_id"]},
        ).scalar_one()
    assert tuple(operation) == ("closed", outcome, code)
    assert active == 0
