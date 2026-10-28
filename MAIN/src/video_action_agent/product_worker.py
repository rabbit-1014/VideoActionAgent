"""Operator-enabled worker for explicit local method-list tasks only."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import text


class ProductTaskWorker:
    def __init__(self, runtime, pipeline, owner, source_modes=None):
        if not owner:
            raise ValueError("Task owner required")
        self.runtime = runtime
        self.pipeline = pipeline
        self.owner = owner
        if source_modes is not None and (
            not source_modes or any(mode not in {"subtitle", "audio_transcription"} for mode in source_modes)
        ):
            raise ValueError("Supported source modes required")
        self.source_modes = tuple(source_modes) if source_modes is not None else None
        self.cooldown = {}
        self.cooldown_versions = {}

    def eligible_task_ids(self, limit=10):
        """Never enlist legacy/diagnostic tasks merely because they have work."""
        with self.runtime.engine.connect() as conn:
            return list(conn.execute(
                text("""
                SELECT DISTINCT t.task_id FROM tasks t
                JOIN task_contracts c ON c.task_id=t.task_id AND c.version=t.current_version
                JOIN work_items w ON w.task_id=t.task_id
                  AND w.contract_version=t.current_version
                WHERE t.owner_id=:owner AND c.input->>'output_kind'='method_list'
                  AND w.expected_revision=t.revision
                  AND (:all_modes OR COALESCE(c.input->>'source_mode','subtitle')=ANY(:source_modes))
                  AND ((w.kind='form_goal' AND c.state='forming')
                    OR (w.kind IN ('read_source','build_artifact') AND c.state='accepted'))
                  AND w.available_at<=clock_timestamp()
                  AND (w.state='ready' OR (w.kind IN ('read_source','build_artifact') AND w.state='leased'
                     AND w.lease_until<=clock_timestamp()))
                ORDER BY t.task_id LIMIT :limit
                """),
                {"owner": self.owner, "limit": limit,
                 "all_modes": self.source_modes is None,
                 "source_modes": list(self.source_modes or ("subtitle", "audio_transcription"))},
            ).scalars())

    def run_cycle(self, limit=10, on_result=None):
        now = datetime.now(UTC)
        results = []
        for task_id in self.eligible_task_ids(limit):
            before = self.runtime.get(self.owner, task_id)
            version = before["current_version"]
            if (self.cooldown_versions.get(task_id) == version
                    and self.cooldown.get(task_id, now) > now):
                continue
            self.cooldown.pop(task_id, None)
            self.cooldown_versions.pop(task_id, None)
            result = self.pipeline.run_until_idle(task_id)
            results.append({"task_id": str(task_id), "result": result})
            if result.get("state") in {"retryable_failure", "waiting_for_browser"}:
                self.cooldown[task_id] = datetime.now(UTC) + timedelta(minutes=10)
                self.cooldown_versions[task_id] = version
            if on_result is not None:
                after = self.runtime.get(self.owner, task_id)
                if (after["current_version"] == version and not after["completed"]
                        and after["contract_state"] != "canceled"):
                    retry_at = self.cooldown.get(task_id)
                    on_result({"owner": self.owner, "task_id": str(task_id),
                               "contract_version": version, "revision": after["revision"],
                               "result": result,
                               "retry_at": retry_at.isoformat() if retry_at else None})
        return results
