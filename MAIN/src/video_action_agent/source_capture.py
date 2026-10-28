"""One local source-only job at a time; durable bundles, no automatic replays."""

import threading
from copy import deepcopy
from pathlib import Path
from uuid import UUID

from .content_intake import IntakeError, capture
from .local_job_store import JobStoreError, LocalJobStore
from .source_access import SourceAccessError, VideoRef
from .source_candidate_bundle import read_bundle
from .source_preview import load_source_preview

FAILURE_CODES = frozenset({
    "source_unavailable", "source_mismatch", "duration_limit", "timeout", "worker_unavailable",
    "worker_closed", "local_runtime_unavailable", "decode_failed", "empty_audio",
    "no_speech_recognized", "transcription_failed", "transcription_timeout",
    "invalid_worker_result", "source_bundle_unavailable", "intake_failed",
    "intake_storage_unavailable",
})
ACTIVE = {"queued", "running", "cancel_requested"}


class SourceCapture:
    def __init__(self, owner, output_root, *, capture_fn=capture):
        if not owner:
            raise ValueError("Explicit owner required")
        if Path(output_root).is_symlink() or Path(output_root).is_junction():
            raise ValueError("Local output root required")
        self.owner = owner
        self.output_root = Path(output_root).resolve()
        if str(self.output_root).startswith(("\\\\", "//")):
            raise ValueError("Local output root required")
        self._capture = capture_fn
        self._lock = threading.RLock()
        self._jobs = {}
        self._store = LocalJobStore(self.output_root, owner)
        self._closed = False

    def _sync(self, job, **changes):
        view = {**job["view"], **changes}
        try:
            self._store.save(view["job_id"], view, job["request"])
        except JobStoreError:
            raise IntakeError("intake_storage_unavailable") from None
        job["view"] = view

    def _restore(self, record):
        view, request = deepcopy(record["view"]), record["request"]
        if (set(request) != {"bvid", "page", "allow_local_audio_read"}
                or request["allow_local_audio_read"] is not True
                or (view.get("bvid"), view.get("page")) != (request["bvid"], request["page"])
                or type(request["page"]) is not int):
            raise IntakeError("intake_not_found")
        ref = VideoRef.parse(f"https://www.bilibili.com/video/{request['bvid']}?p={request['page']}")
        preview = None
        if view["state"] == "available":
            target = self.output_root / view["job_id"]
            if target.is_symlink() or target.is_junction() or target.resolve().parent != self.output_root:
                raise IntakeError("intake_not_found")
            preview = load_source_preview(target)
            if ((preview["source_ref"]["bvid"], preview["source_ref"]["page"]) != (ref.bvid, ref.page)
                    or preview["acquisition"] not in {"live_anonymous_audio", "recorded_audio"}):
                raise IntakeError("intake_not_found")
        elif view["state"] in ACTIVE:
            view.update(state="interrupted", error_code="previous_run_unconfirmed")
        elif view["state"] not in {"failed", "canceled", "interrupted"}:
            raise IntakeError("intake_not_found")
        return {"view": view, "request": deepcopy(request), "candidate": preview,
                "cancel": threading.Event()}

    def _job(self, job_id):
        job = self._jobs.get(str(job_id))
        if job is None:
            record = self._store.load(job_id)
            if record is None:
                raise IntakeError("intake_not_found")
            try:
                job = self._restore(record)
            except (ValueError, KeyError, TypeError):
                raise IntakeError("intake_not_found") from None
            self._jobs[str(job_id)] = job
        return job

    def history(self):
        with self._lock:
            records = self._store.records()
            rows = []
            for record in records:
                try:
                    job = self._jobs.get(record["job_id"]) or self._restore(record)
                    rows.append(self._view(job))
                except (ValueError, KeyError, TypeError):
                    continue
            return {"jobs": rows, "truncated": len(records) >= 500}

    @staticmethod
    def _view(job):
        return {**deepcopy(job["view"]), "formal_task": False, "completed": False,
                "result_available": job["view"]["state"] == "available"}

    def current(self):
        with self._lock:
            job = next(reversed(self._jobs.values()), None)
            if job is None:
                rows = self.history()["jobs"]
                if rows:
                    job = self._job(rows[0]["job_id"])
            return {"enabled": not self._closed, "job": self._view(job) if job else None}

    def start(self, request_id, source, *, allow_local_audio_read=False):
        if allow_local_audio_read is not True:
            raise IntakeError("audio_permission_required")
        try:
            job_id = str(UUID(str(request_id)))
            ref = VideoRef.parse(source)
        except (ValueError, SourceAccessError):
            raise IntakeError("invalid_source") from None
        with self._lock:
            if self._closed:
                raise IntakeError("intake_stopped")
            if job_id in self._jobs or self._store.load(job_id) is not None:
                job = self._job(job_id)
                if (job["view"]["bvid"], job["view"]["page"]) != (ref.bvid, ref.page):
                    raise IntakeError("request_conflict")
                return self._view(job)
            if any(job["view"]["state"] in ACTIVE for job in self._jobs.values()):
                raise IntakeError("intake_busy")
            if len(self._jobs) >= 32:
                raise IntakeError("intake_capacity")
            job = {"view": {"job_id": job_id, "state": "queued", "stage": None,
                            "bvid": ref.bvid, "page": ref.page, "error_code": None},
                   "request": {"bvid": ref.bvid, "page": ref.page, "allow_local_audio_read": True},
                   "cancel": threading.Event(), "candidate": None}
            self._sync(job)
            thread = threading.Thread(target=self._run, args=(job, ref), daemon=True)
            job["thread"] = thread
            self._jobs[job_id] = job
            try:
                thread.start()
            except RuntimeError:
                self._sync(job, state="failed", error_code="intake_failed")
            return self._view(job)

    def _run(self, job, ref):
        def stage(value):
            if job["cancel"].is_set():
                raise IntakeError("cancelled")
            if value not in {"resolving", "downloading", "transcribing", "saving"}:
                raise IntakeError("intake_failed")
            with self._lock:
                if not job["cancel"].is_set():
                    self._sync(job, state="running", stage=value)

        try:
            stage("resolving")
            source = f"https://www.bilibili.com/video/{ref.bvid}?p={ref.page}"
            output = self.output_root / job["view"]["job_id"]
            self._capture(source, output, allow_local_audio_read=True,
                          cancel_check=job["cancel"].is_set, on_stage=stage)
            if job["cancel"].is_set():
                raise IntakeError("cancelled")
            preview = load_source_preview(output)
            identity = preview["source_ref"]
            if ((identity["bvid"], identity["page"]) != (ref.bvid, ref.page)
                    or preview["acquisition"] != "live_anonymous_audio"):
                raise IntakeError("source_mismatch")
            with self._lock:
                if job["cancel"].is_set():
                    self._sync(job, state="canceled")
                else:
                    self._sync(job, state="available")
                    job["candidate"] = preview
        except Exception as exc:  # noqa: BLE001 -- no raw source/SDK paths or secrets in HTTP
            code = getattr(exc, "code", None)
            if not isinstance(code, str):
                code = "intake_failed"
            with self._lock:
                changes = ({"state": "canceled"} if job["cancel"].is_set()
                           or code in {"cancelled", "transcription_cancelled"}
                           else {"state": "failed", "error_code": code if code in FAILURE_CODES
                                 else "intake_failed"})
                try:
                    self._sync(job, **changes)
                except IntakeError:
                    job["view"].update(state="failed", error_code="intake_storage_unavailable")

    def get(self, job_id):
        with self._lock:
            return self._view(self._job(job_id))

    def candidate(self, job_id):
        with self._lock:
            job = self._job(job_id)
            if job["view"]["state"] != "available":
                raise IntakeError("intake_not_ready")
            return deepcopy(job["candidate"])

    def bundle(self, job_id):
        """Return the full validated source, never the display-only projection."""
        with self._lock:
            job = self._job(job_id)
            if job["view"]["state"] != "available":
                raise IntakeError("intake_not_ready")
            target = self.output_root / job["view"]["job_id"]
            if target.is_symlink() or target.is_junction() or target.resolve().parent != self.output_root:
                raise IntakeError("intake_not_ready")
            bundle = read_bundle(target)
            proof = bundle["source_envelope"]["source_evidence"]
            if (proof["bvid"], proof["page"]) != (job["view"]["bvid"], job["view"]["page"]):
                raise IntakeError("source_mismatch")
            return bundle

    def cancel(self, job_id):
        with self._lock:
            job = self._job(job_id)
            if job["view"]["state"] in ACTIVE:
                self._sync(job, state="cancel_requested")
                job["cancel"].set()
            return self._view(job)

    def wait(self, job_id, timeout=2):
        with self._lock:
            thread = self._job(job_id).get("thread")
        if thread is not None and thread.ident is not None:
            thread.join(timeout)

    def close(self):
        with self._lock:
            self._closed = True
            for job in self._jobs.values():
                if job["view"]["state"] in ACTIVE:
                    try:
                        self._sync(job, state="cancel_requested")
                    except IntakeError:
                        pass  # No acknowledged durable cancellation; never restore by replaying.
                    job["cancel"].set()
                    job["view"]["state"] = "cancel_requested"
            active = [job["view"]["job_id"] for job in self._jobs.values()
                      if job["view"]["state"] in ACTIVE]
        for job_id in active:
            self.wait(job_id, 2)
