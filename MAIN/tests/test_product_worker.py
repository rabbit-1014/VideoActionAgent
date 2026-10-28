from video_action_agent.product_worker import ProductTaskWorker


def test_local_product_worker_only_enlists_explicit_method_list_tasks(runtime):
    legacy = runtime.submit("alice", "legacy-task", {"message": "Inspect video"})
    scoped = runtime.submit(
        "alice", "scoped-product-task",
        {"message": "整理方法", "source": "BV1gD421u75J", "output_kind": "method_list",
         "local_browser_actions": ["subtitle"]},
    )

    class FakePipeline:
        def run_until_idle(self, task_id):
            assert task_id == scoped["task_id"]
            return {"stage": "formation", "decision": "accept"}

    worker = ProductTaskWorker(runtime, FakePipeline(), "alice")
    assert worker.eligible_task_ids() == [scoped["task_id"]]
    assert worker.run_cycle() == [{"task_id": str(scoped["task_id"]),
                                   "result": {"stage": "formation", "decision": "accept"}}]
    assert legacy["task_id"] not in worker.eligible_task_ids()
