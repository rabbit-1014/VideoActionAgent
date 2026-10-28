"""Owner-bound saved opinions; declared field grid or legacy two-stage review."""

import hashlib
import json
import re
import threading
from copy import deepcopy
from uuid import UUID

from .local_job_store import JobStoreError, LocalJobStore
from .method_source_review import (
    evaluate_method_review,
    prepare_method_requirements,
    validate_method_requirements,
    validate_method_review,
)
from .notes_requirements import (
    NotesRequirementsError,
    evaluate_candidate,
    freeze_requirements,
    validate_evaluation,
    validate_requirements,
)
from .notes_service import NotesServiceError

ACTIVE = {"queued", "forming_requirements", "checking"}
STATES = ACTIVE | {"available", "failed", "canceled", "interrupted"}
MAX_CHECKS = 32
VIEW_KEYS = {"job_id", "candidate_id", "state", "error_code", "result_available"}
REQUEST_KEYS = {"schema_version", "snapshot", "allow_model_text", "requirements",
                "evaluation", "result_digest"}


class NotesCheckError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _digest(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _identity(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise NotesCheckError("invalid_check_request") from None


class NotesChecks:
    def __init__(self, notes_service, provider_factory):
        if not callable(provider_factory):
            raise TypeError("Explicit check provider required")
        self.notes = notes_service
        self.owner = notes_service.owner
        self._provider = provider_factory
        self._store = LocalJobStore(notes_service.output_root / ".checks", self.owner)
        self.scope_id = _digest({"owner": self.owner, "root": str(self._store.root)})
        self._jobs = {}
        self._lock = threading.RLock()
        self._closed = False

    def _sync(self, job, *, state=None, error_code=None, **updates):
        view, request = deepcopy(job["view"]), deepcopy(job["request"])
        request.update(updates)
        if state is not None:
            view.update(state=state, error_code=error_code,
                        result_available=state == "available")
        self._store.save(view["job_id"], view, request)
        job["view"], job["request"] = view, request

    def _restore(self, row):
        view, request = row["view"], row["request"]
        try:
            if (set(view) != VIEW_KEYS or set(request) != REQUEST_KEYS
                    or view["state"] not in STATES
                    or _identity(view["candidate_id"]) != view["candidate_id"]
                    or type(view["result_available"]) is not bool
                    or view["result_available"] != (view["state"] == "available")
                    or request["schema_version"] not in (1, 2)
                    or type(request["schema_version"]) is not int
                    or request["allow_model_text"] is not True
                    or request["snapshot"]["candidate_id"] != view["candidate_id"]):
                raise ValueError()
            if request["schema_version"] == 2:
                snapshot = request["snapshot"]
                if not isinstance(snapshot["sections"], list):
                    raise ValueError()
                validate_method_requirements(request["requirements"], snapshot["user_request"],
                                             snapshot["amendments"])
        except (ValueError, TypeError, KeyError):
            raise NotesCheckError("check_storage_unavailable") from None
        job = {"view": deepcopy(view), "request": deepcopy(request),
               "cancel": threading.Event()}
        if view["state"] in ACTIVE:
            self._sync(job, state="interrupted", error_code="check_interrupted")
        return job

    def _inventory(self):
        rows = self._store.records(strict=True)
        for row in rows:
            identity = row["job_id"]
            if identity not in self._jobs:
                self._jobs[identity] = self._restore(row)
        return rows

    def _job(self, identity):
        identity = _identity(identity)
        if identity not in self._jobs:
            row = self._store.load(identity)
            if row is None:
                raise NotesCheckError("check_unavailable")
            self._jobs[identity] = self._restore(row)
        return self._jobs[identity]

    def current(self, candidate_id=None):
        with self._lock:
            rows = self._inventory()
            active = [job for job in self._jobs.values() if job["view"]["state"] in ACTIVE]
            if active:
                return deepcopy(active[0]["view"])
            for row in rows:
                view = self._jobs[row["job_id"]]["view"]
                if candidate_id is None or view["candidate_id"] == str(candidate_id):
                    return deepcopy(view)
            return None

    def _snapshot_matches(self, current, snapshot, *, is_method):
        if _digest(current) == _digest(snapshot):
            return True
        if is_method or current.get("output_kind") != "source_answer":
            return False
        # Preserve legacy opinions, without accepting arbitrary old text. Rebuild
        # the sole previous representation from the validated immutable answer;
        # identity, source, amendments and every other field still must match.
        legacy = deepcopy(current)
        answer = self.notes.candidate(current["candidate_id"])["candidate"]
        legacy["candidate_text"] = json.dumps({
            "answer": [item["text"] for item in answer["answer"]],
            "missing": answer["missing"], "unverified": answer["unverified"],
        }, ensure_ascii=False, sort_keys=True, allow_nan=False)
        return _digest(legacy) == _digest(snapshot)

    def start(self, request_id, candidate_id, *, allow_model_text=False):
        if allow_model_text is not True:
            raise NotesCheckError("model_text_permission_required")
        identity, target = _identity(request_id), _identity(candidate_id)
        with self._lock:
            if self._closed:
                raise NotesCheckError("check_service_closed")
            rows = self._inventory()
            if identity in self._jobs:
                job = self._jobs[identity]
                snapshot = (self.notes.inspection_context(target, include_sections=True)
                            if job["request"]["schema_version"] == 2
                            else self.notes.inspection_context(target))
                if not self._snapshot_matches(snapshot, job["request"]["snapshot"],
                                              is_method=job["request"]["schema_version"] == 2):
                    raise NotesCheckError("check_request_conflict")
                return deepcopy(job["view"])
            if any(job["view"]["state"] in ACTIVE or
                   (job.get("thread") is not None and job["thread"].is_alive())
                   for job in self._jobs.values()):
                raise NotesCheckError("check_in_progress")
            if len(rows) >= MAX_CHECKS:
                raise NotesCheckError("check_capacity_reached")
            snapshot = self.notes.inspection_context(target)
            requirements = (prepare_method_requirements(snapshot["user_request"], snapshot["amendments"])
                            if snapshot.get("output_kind", "structured_notes") == "structured_notes"
                            else None)
            schema_version = 1
            if requirements is not None:
                schema_version = 2
                snapshot = self.notes.inspection_context(target, include_sections=True)
                validate_method_requirements(requirements, snapshot["user_request"], snapshot["amendments"])
            job = {"view": {"job_id": identity, "candidate_id": target, "state": "queued",
                            "error_code": None, "result_available": False},
                   "request": {"schema_version": schema_version, "snapshot": snapshot,
                               "allow_model_text": True, "requirements": requirements,
                               "evaluation": None, "result_digest": None},
                   "cancel": threading.Event()}
            # Persistent intent and exact target exist before any provider construction/call.
            self._sync(job)
            self._jobs[identity] = job
            thread = threading.Thread(target=self._run, args=(job,), daemon=True)
            job["thread"] = thread
            thread.start()
            return deepcopy(job["view"])

    def _live(self, job):
        return not self._closed and not job["cancel"].is_set()

    def _run(self, job):
        try:
            with self._lock:
                if not self._live(job):
                    return
                self._sync(job, state="forming_requirements")
            context = job["request"]["snapshot"]
            is_method = job["request"]["schema_version"] == 2
            if is_method:
                requirements = validate_method_requirements(job["request"]["requirements"],
                                                            context["user_request"], context["amendments"])
            else:
                provider = self._provider()
                requirements = freeze_requirements(context["user_request"], context["amendments"], provider)
            with self._lock:
                if not self._live(job):
                    return
                # Freeze and persist independently derived requirements before review dispatch.
                self._sync(job, state="checking", requirements=requirements)
            if is_method:
                provider = self._provider()
                with self._lock:
                    if not self._live(job):
                        return
                evaluation = evaluate_method_review(requirements, context["sections"],
                                                    context["segments"], provider)
            else:
                evaluation = evaluate_candidate(requirements, context["candidate_text"],
                                                context["segments"], provider)
            with self._lock:
                if not self._live(job):
                    return
                current = (self.notes.inspection_context(context["candidate_id"], include_sections=True)
                           if is_method else self.notes.inspection_context(context["candidate_id"]))
                if _digest(current) != _digest(context):
                    raise NotesCheckError("candidate_changed")
                result_digest = _digest({"requirements": requirements, "evaluation": evaluation})
                self._sync(job, state="available", evaluation=evaluation, result_digest=result_digest)
        except Exception as exc:  # noqa: BLE001 -- no raw provider/input/path in public failures
            with self._lock:
                if not self._live(job):
                    return
                code = (exc.code if isinstance(exc, (NotesCheckError, NotesRequirementsError,
                                                     NotesServiceError, JobStoreError))
                        else "model_unavailable")
                if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", code):
                    code = "check_failed"
                try:
                    self._sync(job, state="failed", error_code=code)
                except (JobStoreError, NotesCheckError):
                    job["view"].update(state="interrupted", error_code="check_storage_unavailable",
                                       result_available=False)

    def get(self, identity):
        with self._lock:
            job = self._job(identity)
            if job["view"]["state"] == "available":
                self._result(job)
            return deepcopy(job["view"])

    def _result(self, job):
        if job["view"]["state"] != "available":
            raise NotesCheckError("check_not_ready")
        request = job["request"]
        snapshot = request["snapshot"]
        is_method = request["schema_version"] == 2
        current = (self.notes.inspection_context(job["view"]["candidate_id"], include_sections=True)
                   if is_method else self.notes.inspection_context(job["view"]["candidate_id"]))
        if not self._snapshot_matches(current, snapshot, is_method=is_method):
            raise NotesCheckError("candidate_changed")
        if is_method:
            requirements = validate_method_requirements(request["requirements"], snapshot["user_request"],
                                                        snapshot["amendments"])
            evaluation = validate_method_review(request["evaluation"], requirements,
                                                snapshot["sections"], snapshot["segments"])
        else:
            requirements = validate_requirements(request["requirements"], snapshot["user_request"],
                                                snapshot["amendments"])
            evaluation = validate_evaluation(request["evaluation"], requirements,
                                             snapshot["candidate_text"], snapshot["segments"])
        if request["result_digest"] != _digest({"requirements": requirements, "evaluation": evaluation}):
            raise NotesCheckError("check_storage_unavailable")
        return deepcopy({"candidate_id": snapshot["candidate_id"],
                         "candidate_fingerprint": snapshot["record_fingerprint"],
                         "requirements": requirements, "evaluation": evaluation,
                         "formal_task": False, "completed": False, "quality_approved": False})

    def result(self, identity):
        with self._lock:
            return self._result(self._job(identity))

    def cancel(self, identity):
        with self._lock:
            job = self._job(identity)
            if job["view"]["state"] in ACTIVE:
                self._sync(job, state="canceled")
                job["cancel"].set()
            return deepcopy(job["view"])

    def wait(self, identity, timeout=2):
        with self._lock:
            thread = self._job(identity).get("thread")
        if thread is not None and thread.ident is not None:
            thread.join(timeout)

    def close(self):
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                if job["view"]["state"] in ACTIVE:
                    job["cancel"].set()
                    try:
                        self._sync(job, state="interrupted", error_code="check_interrupted")
                    except JobStoreError:
                        pass
