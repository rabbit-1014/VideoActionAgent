"""Frozen local intent connecting source and notes jobs; restart never replays."""

import hashlib
import json
import threading
from copy import deepcopy
from uuid import UUID, uuid5

from .content_intake import IntakeError
from .local_job_store import JobStoreError, LocalJobStore
from .notes_service import OUTPUT_FILES, NotesServiceError
from .source_access import SourceAccessError, VideoRef

ACTIVE = {"queued", "extracting", "building"}
TERMINAL = {"available", "failed", "canceled", "interrupted"}
CHILD_ACTIVE = {"queued", "running", "cancel_requested"}


class FlowError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class LocalFlow:
    def __init__(self, sources, notes):
        if sources.owner != notes.owner or notes._capture is not sources:
            raise ValueError("Same owner and source service required")
        self.owner = sources.owner
        self.sources, self.notes = sources, notes
        self._store = LocalJobStore(notes.output_root / ".flows", self.owner)
        self._jobs = {}
        self._lock = threading.RLock()
        self._closed = False
        self.scope_id = hashlib.sha256(json.dumps(
            [self.owner, str(sources.output_root), str(notes.output_root)],
            ensure_ascii=False).encode()).hexdigest()

    @staticmethod
    def _request(request_id, source, user_request, output_kind="structured_notes", *,
                 allow_local_audio_read=False, allow_model_text=False):
        if allow_local_audio_read is not True or allow_model_text is not True:
            raise FlowError("flow_permission_required")
        try:
            identity = str(UUID(str(request_id)))
            ref = VideoRef.parse(source)
        except (ValueError, TypeError, SourceAccessError):
            raise FlowError("invalid_flow_request") from None
        if (not isinstance(user_request, str) or not 1 <= len(user_request.strip()) <= 4000
                or not isinstance(output_kind, str) or output_kind not in OUTPUT_FILES):
            raise FlowError("invalid_flow_request")
        return {"request_id": identity,
                "source": f"https://www.bilibili.com/video/{ref.bvid}/?p={ref.page}",
                "user_request": user_request.strip(), "output_kind": output_kind,
                "allow_local_audio_read": True, "allow_model_text": True}, ref

    @staticmethod
    def _base(request, ref):
        identity = request["request_id"]
        return {"job_id": identity, "flow_id": identity,
                "source_job_id": str(uuid5(UUID(identity), "source")),
                "notes_job_id": str(uuid5(UUID(identity), "notes")),
                "bvid": ref.bvid, "page": ref.page,
                "output_kind": request["output_kind"], "user_request": request["user_request"],
                "state": "queued", "error_code": None}

    def _sync(self, job, **changes):
        view = {**job["view"], **changes}
        self._store.save(view["flow_id"], view, job["request"])
        job["view"] = view

    def _job(self, identity):
        try:
            identity = str(UUID(str(identity)))
        except (ValueError, TypeError):
            raise FlowError("flow_not_found") from None
        if identity not in self._jobs:
            record = self._store.load(identity)
            if record is None:
                raise FlowError("flow_not_found")
            try:
                request, ref = self._request(**record["request"])
                expected = self._base(request, ref)
                view = record["view"]
                if (request["request_id"] != identity or set(view) != set(expected)
                        or any(view[key] != value for key, value in expected.items()
                               if key not in {"state", "error_code"})
                        or view["state"] not in ACTIVE | TERMINAL):
                    raise ValueError("Invalid flow")
                self._jobs[identity] = {"view": deepcopy(view), "request": request,
                                        "stop": threading.Event(), "restored": True}
            except (ValueError, TypeError, KeyError):
                raise FlowError("flow_not_found") from None
        return self._jobs[identity]

    @staticmethod
    def _child(service, identity):
        try:
            return service.get(identity)
        except (IntakeError, NotesServiceError):
            return None

    def _view(self, job):
        view = deepcopy(job["view"])
        source = self._child(self.sources, view["source_job_id"])
        notes = self._child(self.notes, view["notes_job_id"])
        view.update(source_state=source["state"] if source else None,
                    notes_state=notes["state"] if notes else None)
        valid = not source or (source["bvid"], source["page"]) == (view["bvid"], view["page"])
        if notes:
            valid = valid and (notes["bvid"], notes["page"], notes.get("output_kind")) == (
                view["bvid"], view["page"], view["output_kind"])
            try:
                valid = valid and self.notes.matches_request(
                    view["notes_job_id"], view["source_job_id"], view["user_request"],
                    view["output_kind"])
            except NotesServiceError:
                valid = False
        if view["state"] == "canceled":
            pass
        elif not valid:
            view.update(state="failed", error_code="flow_child_mismatch")
        elif notes and notes["state"] == "available":
            view.update(state="available", error_code=None)
        elif view["state"] == "available":
            view.update(state="interrupted", error_code="flow_child_unavailable")
        elif view["state"] in {"failed", "interrupted"}:
            pass
        elif notes and notes["state"] in {"failed", "canceled", "interrupted"}:
            view.update(state=notes["state"], error_code=notes.get("error_code"))
        elif source and source["state"] in {"failed", "canceled", "interrupted"}:
            view.update(state=source["state"], error_code=source.get("error_code"))
        elif job["restored"]:
            view.update(state="interrupted", error_code="previous_run_unconfirmed")
        return {key: value for key, value in {
            **view, "formal_task": False, "completed": False,
            "result_available": view["state"] == "available"}.items() if key != "job_id"}

    def get(self, identity):
        with self._lock:
            return self._view(self._job(identity))

    def history(self):
        with self._lock:
            records = self._store.records()
            rows = []
            for record in records:
                try:
                    rows.append(self._view(self._job(record["job_id"])))
                except FlowError:
                    continue
            return {"flows": rows, "truncated": len(records) >= 500}

    def current(self):
        with self._lock:
            rows = self.history()["flows"]
            return {"enabled": not self._closed, "scope_id": self.scope_id,
                    "flow": rows[0] if rows else None}

    def start(self, request_id, source, user_request, output_kind="structured_notes", *,
              allow_local_audio_read=False, allow_model_text=False):
        request, ref = self._request(request_id, source, user_request, output_kind,
                                    allow_local_audio_read=allow_local_audio_read,
                                    allow_model_text=allow_model_text)
        with self._lock:
            if self._closed:
                raise FlowError("flow_stopped")
            identity = request["request_id"]
            # Corrupt intent inventory is not proof that this request never existed.
            self._store.records(strict=True)
            if identity in self._jobs or self._store.load(identity) is not None:
                job = self._job(identity)
                if job["request"] != request:
                    raise FlowError("flow_request_conflict")
                return self._view(job)
            source_current = self.sources.current()["job"]
            notes_current = self.notes.current()["job"]
            if (any(self._view(job)["state"] in ACTIVE for job in self._jobs.values())
                    or source_current and source_current["state"] in CHILD_ACTIVE
                    or notes_current and notes_current["state"] in CHILD_ACTIVE):
                raise FlowError("flow_busy")
            if len(self._jobs) >= 32:
                raise FlowError("flow_capacity")
            job = {"view": self._base(request, ref), "request": request,
                   "stop": threading.Event(), "restored": False}
            self._sync(job)
            thread = threading.Thread(target=self._run, args=(job,), daemon=True)
            job["thread"] = thread
            self._jobs[identity] = job
            try:
                thread.start()
            except RuntimeError:
                self._sync(job, state="failed", error_code="flow_start_failed")
            return self._view(job)

    def _run(self, job):
        try:
            with self._lock:
                if job["stop"].is_set():
                    return
                self._sync(job, state="extracting")
                source_id = job["view"]["source_job_id"]
                self.sources.start(source_id, job["request"]["source"],
                                   allow_local_audio_read=True)
            while not job["stop"].is_set():
                self.sources.wait(source_id, 0.1)
                source = self.sources.get(source_id)
                if source["state"] not in CHILD_ACTIVE:
                    break
            with self._lock:
                if job["stop"].is_set():
                    return
                if source["state"] != "available":
                    state = source["state"] if source["state"] in TERMINAL else "failed"
                    self._sync(job, state=state, error_code=source.get("error_code"))
                    return
                self._sync(job, state="building")
                notes_id = job["view"]["notes_job_id"]
                self.notes.start(notes_id, source_id, job["request"]["user_request"],
                                 output_kind=job["request"]["output_kind"], allow_model_text=True)
            while not job["stop"].is_set():
                self.notes.wait(notes_id, 0.1)
                notes = self.notes.get(notes_id)
                if notes["state"] not in CHILD_ACTIVE:
                    break
            with self._lock:
                if not job["stop"].is_set():
                    self._sync(job, state=notes["state"], error_code=notes.get("error_code"))
        except (IntakeError, NotesServiceError, JobStoreError) as exc:
            with self._lock:
                if not job["stop"].is_set():
                    try:
                        self._sync(job, state="failed", error_code=exc.code)
                    except JobStoreError:
                        job["view"].update(state="interrupted", error_code="job_storage_unavailable")

    def cancel(self, identity):
        with self._lock:
            job = self._job(identity)
            if self._view(job)["state"] in ACTIVE:
                self._sync(job, state="canceled", error_code=None)
                job["stop"].set()
                self._cancel_children(job)
            return self._view(job)

    def _cancel_children(self, job):
        for service, field in ((self.sources, "source_job_id"), (self.notes, "notes_job_id")):
            try:
                service.cancel(job["view"][field])
            except (IntakeError, NotesServiceError):
                pass

    def close(self):
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                if self._view(job)["state"] in ACTIVE:
                    job["stop"].set()
                    try:
                        self._sync(job, state="interrupted", error_code="previous_run_unconfirmed")
                    except JobStoreError:
                        job["view"].update(state="interrupted", error_code="job_storage_unavailable")
                    self._cancel_children(job)
