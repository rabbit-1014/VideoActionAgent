"""Reuse NotesService builders/skills, with current-source and single-dispatch observation."""
import json
import threading
import time

from bridge_contract import atomic
from video_action_agent.notes_service import NotesService, NotesServiceError

from method_layout import normalize_layout


class TypedOutputs(NotesService):
    def __init__(self, owner, root, sources, factory, checkpoint):
        self.context = threading.local()
        self.factory, self.checkpoint = factory, checkpoint
        self.traces = root / ".model-traces"
        self.traces.mkdir(parents=True, exist_ok=True)
        super().__init__(owner, root, self._observed_provider, source_capture=sources)

    def _run(self, job):
        self.context.identity = job["view"]["job_id"]
        super()._run(job)

    def _observed_provider(self):
        service, identity = self, self.context.identity
        original = self.factory()
        class Provider:
            model_id = original.model_id
            def complete(self, system, user):
                trace = service.traces / (identity + ".json")
                replay = getattr(original, "replay_origin", None)
                calls = 0 if replay else 1
                with service._lock:
                    service._checkpoint(identity)
                    if trace.exists():
                        raise NotesServiceError("previous_model_attempt_unconfirmed")
                    atomic(trace, {"state": "reusing_saved_reply" if replay else "dispatching", "model_calls": calls,
                                   "model_id": self.model_id, "replay_origin": replay, "automatic_retry": False})
                    atomic(service.traces / (identity + ".request.json"), {"system": system, "user": user})
                started = time.monotonic()
                try:
                    result = original.complete(system, user)
                except Exception as exc:  # noqa: BLE001 -- raw provider messages can contain credentials
                    code = getattr(exc, "code", None)
                    state = "rejected" if code in {"api_key_missing", "http_error"} else "outcome_unknown"
                    atomic(trace, {"state": state, "model_calls": calls,
                                   "model_id": self.model_id, "error_type": type(exc).__name__, "automatic_retry": False})
                    raise NotesServiceError("model_outcome_unknown" if state == "outcome_unknown" else "model_rejected") from None
                atomic(service.traces / (identity + ".reply.json"), {"text": result})
                atomic(trace, {"state": "reply_received", "model_calls": calls,
                               "model_id": self.model_id, "seconds": time.monotonic() - started,
                               "replay_origin": replay, "automatic_retry": False})
                service._checkpoint(identity)
                normalized, proof = normalize_layout(result, service._jobs[identity]["view"]["output_kind"])
                if proof:
                    atomic(service.traces / (identity + ".layout-normalization.json"), proof)
                return normalized
        return Provider()

    def _checkpoint(self, identity):
        job = self._jobs.get(identity)
        if job and job["cancel"].is_set():
            raise NotesServiceError("canceled")
        self.checkpoint(identity)

    def _save(self, job, candidate, markdown):
        self._checkpoint(job["view"]["job_id"])
        return super()._save(job, candidate, markdown)

    def trace(self, identity):
        path = self.traces / (identity + ".json")
        return json.loads(path.read_bytes()) if path.is_file() else {"model_calls": 0}
