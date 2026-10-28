"""Local source-bundle processing; saved candidates, never formal task completion."""

import hashlib
import json
import re
import tempfile
import threading
from copy import deepcopy
from functools import partial
from pathlib import Path
from uuid import UUID

from .artifact_revision import MAX_LINEAGE, AmendmentProvider, checked_revision, lineage
from .local_job_store import JobStoreError, LocalJobStore, job_directories
from .source_candidate_bundle import MAX_FILE_BYTES, _validate
from .task_skills import SkillProvider, load_skill, render_reading, stored_skill

ACTIVE = {"queued", "running", "cancel_requested"}
OUTPUT_FILES = {"structured_notes": "notes.md", "method_list": "method-list.md",
                "source_answer": "answer.md"}


class NotesServiceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _source(bundle, output_kind="structured_notes"):
    _validate(bundle)
    envelope = bundle["source_envelope"]
    proof = envelope["source_evidence"]
    ref = {"kind": "audio_transcription", **{key: proof[key] for key in
           ("bvid", "page", "cid", "part_title")}}
    if output_kind == "method_list":
        from .source_access import VideoRef
        from .transcribed_source import material_source_ref, parse_transcribed_reply

        material = parse_transcribed_reply(VideoRef(proof["bvid"], proof["page"]),
                                           _json(envelope), proof)
        ref = material_source_ref({**material, "action": "audio_transcription",
                                   "bvid": proof["bvid"], "page": proof["page"]})
    return ref, envelope["transcription"]["segments"], _digest(bundle)


def _signature(source_id, digest, request, output_kind, skill=None, revision=None):
    value = {"source_id": source_id, "source_digest": digest, "user_request": request}
    if output_kind != "structured_notes":
        value["output_kind"] = output_kind
    if skill is not None:
        value["skill"] = skill.metadata()
    if revision is not None:
        value["revision"] = checked_revision(revision)
    return _digest(value)


def _functions(output_kind):
    if output_kind == "source_answer":
        from .source_answer_builder import (
            build_source_answer,
            render_source_answer,
            validate_source_answer,
        )

        return build_source_answer, validate_source_answer, render_source_answer
    if output_kind == "method_list":
        from .artifact_builder import build_method_list, render_markdown, validate_method_list

        return (partial(build_method_list, general_context=True, derive_timing=True),
                partial(validate_method_list, general_context=True), render_markdown)
    from .notes_builder import build_notes, render_notes, validate_notes

    return build_notes, validate_notes, render_notes


class NotesService:
    def __init__(self, owner, output_root, provider_factory, *, saved_bundle=None,
                 source_capture=None):
        if not owner or not callable(provider_factory):
            raise ValueError("Explicit owner and provider required")
        if Path(output_root).is_symlink() or Path(output_root).is_junction():
            raise ValueError("Local output root required")
        self.owner = owner
        self.output_root = Path(output_root).resolve()
        if str(self.output_root).startswith(("\\\\", "//")):
            raise ValueError("Local output root required")
        self._provider = provider_factory
        self._saved = deepcopy(saved_bundle)
        if self._saved is not None:
            _source(self._saved)
        if source_capture is not None and source_capture.owner != owner:
            raise ValueError("Source owner mismatch")
        self._capture = source_capture
        self._jobs = {}
        self._store = LocalJobStore(self.output_root, owner)
        self._lock = threading.RLock()
        self._closed = False

    def _bundle(self, source_id):
        if source_id == "saved" and self._saved is not None:
            return deepcopy(self._saved)
        if isinstance(source_id, str) and source_id.startswith("artifact:"):
            try:
                identity = str(UUID(source_id[9:]))
                record, _ = self._read(identity)
                return deepcopy(record["source_bundle"])
            except (ValueError, TypeError, KeyError):
                raise NotesServiceError("notes_source_unavailable") from None
        try:
            source_id = str(UUID(str(source_id)))
            if self._capture is not None:
                return self._capture.bundle(source_id)
        except (ValueError, LookupError):
            pass
        raise NotesServiceError("notes_source_unavailable")

    def sources(self):
        identities = ["saved"] if self._saved is not None else []
        if self._capture is not None:
            identities.extend(job["job_id"] for job in self._capture.history()["jobs"]
                              if job["state"] == "available")
        identities.extend("artifact:" + row["job_id"] for row in self.history()["results"]
                          if row["state"] == "available")
        results = []
        seen = set()
        for identity in identities:
            try:
                bundle = self._bundle(identity)
                ref, segments, digest = _source(bundle)
                if digest in seen:
                    continue
                seen.add(digest)
                results.append({"source_id": identity, "bvid": ref["bvid"], "page": ref["page"],
                                "cid": ref["cid"], "title": ref["part_title"],
                                "segments": len(segments), "acquisition": bundle["acquisition"]})
            except ValueError:
                continue
        return {"sources": results}

    def _sync(self, job, **changes):
        view = {**job["view"], **changes}
        request = job["journal_request"]
        try:
            self._store.save(view["job_id"], view, request)
        except JobStoreError:
            raise NotesServiceError("notes_storage_unavailable") from None
        job["view"] = view

    def _restore_journal(self, record):
        view, request = deepcopy(record["view"]), deepcopy(record["request"])
        keys = {"source_id", "user_request", "output_kind", "signature",
                "model_text_authorized", "source_digest", "skill"}
        revision = checked_revision(request["revision"]) if "revision" in request else None
        if (set(request) != keys | ({"revision"} if revision else set())
                or request["model_text_authorized"] is not True
                or request["output_kind"] not in OUTPUT_FILES
                or view.get("output_kind") != request["output_kind"]
                or view.get("source_id") != request["source_id"]
                or not isinstance(request["user_request"], str)
                or not 1 <= len(request["user_request"].strip()) <= 4000
                or not isinstance(request["signature"], str)
                or len(request["signature"]) != 64):
            raise NotesServiceError("notes_candidate_unavailable")
        skill = stored_skill(request["skill"], request["output_kind"])
        if skill is None or _signature(request["source_id"], request["source_digest"],
                                       request["user_request"], request["output_kind"], skill,
                                       revision) != request["signature"]:
            raise NotesServiceError("notes_candidate_unavailable")
        if revision:
            parent, _ = self._read(revision["parent_job_id"])
            self._check_parent(revision, parent, request["source_id"], request["source_digest"],
                               request["user_request"], request["output_kind"])
            if any(view.get(key) != revision[key] for key in ("root_job_id", "version", "parent_job_id")):
                raise NotesServiceError("notes_candidate_unavailable")
        if view["state"] in ACTIVE:
            view.update(state="interrupted", error_code="previous_run_unconfirmed")
        elif view["state"] not in {"failed", "canceled", "interrupted"}:
            raise NotesServiceError("notes_candidate_unavailable")
        return {"view": view, "signature": request["signature"], "journal_request": request,
                "cancel": threading.Event(), "skill": request["skill"], "revision": revision}

    def history(self):
        with self._lock:
            directories = job_directories(self.output_root)
            results = []
            seen = set()
            for directory in directories:
                try:
                    record, value = self._read(directory.name)
                    draft = value["candidate"]
                    results.append({"job_id": directory.name, "state": "available",
                                    "output_kind": draft["kind"], "bvid": draft["source_ref"]["bvid"],
                                    "page": draft["source_ref"]["page"],
                                    "title": draft.get("title", record["user_request"]),
                                    "user_request": record["user_request"],
                                    "source_id": "artifact:" + directory.name, "error_code": None,
                                    **lineage(record)})
                    seen.add(directory.name)
                except (ValueError, KeyError, TypeError):
                    continue
            records = self._store.records()
            for record in records:
                if record["job_id"] in seen:
                    continue
                try:
                    job = self._jobs.get(record["job_id"]) or self._restore_journal(record)
                    request = record["request"]
                    results.append({**self._view(job), "title": request["user_request"],
                                    "user_request": request["user_request"]})
                except (ValueError, KeyError, TypeError):
                    continue
            return {"results": results, "truncated": len(directories) >= 500 or len(records) >= 500}

    @staticmethod
    def _view(job):
        return {**deepcopy(job["view"]), "result_available": job["view"]["state"] == "available",
                "output_kind": job["view"].get("output_kind", "structured_notes"),
                "root_job_id": job["view"].get("root_job_id", job["view"]["job_id"]),
                "version": job["view"].get("version", 1),
                "parent_job_id": job["view"].get("parent_job_id"),
                "formal_task": False, "completed": False}

    def current(self):
        with self._lock:
            job = next(reversed(self._jobs.values()), None)
            if job is None:
                rows = self.history()["results"]
                if rows:
                    job = self._job(rows[0]["job_id"])
            return {"enabled": not self._closed, "job": self._view(job) if job else None,
                    "scope_id": _digest({"owner": self.owner, "root": str(self.output_root)})}

    def start(self, request_id, source_id, user_request, *, allow_model_text=False,
              output_kind="structured_notes"):
        if allow_model_text is not True:
            raise NotesServiceError("model_text_permission_required")
        if not isinstance(output_kind, str) or output_kind not in OUTPUT_FILES:
            raise NotesServiceError("invalid_notes_request")
        try:
            job_id = str(UUID(str(request_id)))
        except ValueError:
            raise NotesServiceError("invalid_notes_request") from None
        if not isinstance(user_request, str) or not 1 <= len(user_request.strip()) <= 4000:
            raise NotesServiceError("invalid_notes_request")
        request = user_request.strip()
        with self._lock:
            if self._closed:
                raise NotesServiceError("notes_stopped")
            bundle = self._bundle(source_id)
            ref, segments, digest = _source(bundle, output_kind)
            if (job_id in self._jobs or (self.output_root / job_id).exists()
                    or self._store.load(job_id) is not None):
                job = self._job(job_id)
                if job["view"].get("output_kind", "structured_notes") != output_kind:
                    raise NotesServiceError("notes_request_conflict")
                frozen = stored_skill(job.get("skill"), output_kind)
                signature = _signature(source_id, digest, request, output_kind, frozen)
                if job["signature"] != signature:
                    raise NotesServiceError("notes_request_conflict")
                return self._view(job)
            if any(job["view"]["state"] in ACTIVE for job in self._jobs.values()):
                raise NotesServiceError("notes_busy")
            if len(self._jobs) >= 32:
                raise NotesServiceError("notes_capacity")
            skill = load_skill(output_kind)
            # A request limit, not a tokenizer estimate or silent truncation policy.
            if len(_json(segments).encode("utf-8")) > 200_000:
                raise NotesServiceError("notes_input_too_large")
            return self._enqueue(job_id, source_id, request, output_kind, bundle, ref, digest, skill)

    def _enqueue(self, job_id, source_id, request, output_kind, bundle, ref, digest, skill,
                 revision=None, previous_candidate=None):
        # Called under the service lock, after scope/idempotence/admission checks.
        signature = _signature(source_id, digest, request, output_kind, skill, revision)
        version = ({key: revision[key] for key in ("root_job_id", "version", "parent_job_id")}
                   if revision else {"root_job_id": job_id, "version": 1, "parent_job_id": None})
        job = {"view": {"job_id": job_id, "state": "queued", "stage": None, **version,
                        "source_id": source_id, "bvid": ref["bvid"], "page": ref["page"],
                        "output_kind": output_kind,
                        "error_code": None}, "signature": signature,
               "cancel": threading.Event(), "bundle": bundle, "request": request,
               "skill": skill.record(), "revision": revision,
               "previous_candidate": previous_candidate}
        job["journal_request"] = {"source_id": source_id, "user_request": request,
                                  "output_kind": output_kind, "signature": signature,
                                  "model_text_authorized": True, "source_digest": digest,
                                  "skill": skill.record()}
        if revision:
            job["journal_request"]["revision"] = revision
        self._sync(job)
        thread = threading.Thread(target=self._run, args=(job,), daemon=True)
        job["thread"] = thread
        self._jobs[job_id] = job
        try:
            thread.start()
        except RuntimeError:
            self._sync(job, state="failed", error_code="notes_failed")
        return self._view(job)

    @staticmethod
    def _check_parent(revision, parent, source_id, digest, request, output_kind):
        old = lineage(parent)
        if (revision["parent_record_sha256"] != _digest(parent)
                or revision["root_job_id"] != old["root_job_id"]
                or revision["version"] <= old["version"]
                or source_id != "artifact:" + parent["job"]["job_id"]
                or digest != _source(parent["source_bundle"], output_kind)[2]
                or request != parent["user_request"]
                or output_kind != parent.get("output_kind", "structured_notes")):
            raise NotesServiceError("notes_candidate_unavailable")

    def _family_inventory(self, root_id):
        try:
            directories, records = job_directories(self.output_root), self._store.records(strict=True)
        except JobStoreError:
            raise NotesServiceError("revision_history_incomplete") from None
        if len(directories) >= 500 or len(records) >= 500:
            raise NotesServiceError("revision_history_incomplete")
        versions, published, seen = {}, [], set()
        for directory in directories:
            try:
                record, _ = self._read(directory.name)
            except NotesServiceError:
                # Never silently drop an owned corrupt new head and amend an older version.
                try:
                    path = directory / "candidate.json"
                    if path.is_symlink() or path.is_junction() or path.stat().st_size > MAX_FILE_BYTES:
                        raise ValueError()
                    marker = json.loads(path.read_bytes())
                    if not isinstance(marker, dict):
                        raise TypeError()
                    if isinstance(marker.get("owner"), str) and marker["owner"] != self.owner:
                        continue
                except (OSError, ValueError, TypeError):
                    pass
                raise NotesServiceError("revision_history_incomplete") from None
            seen.add(directory.name)
            metadata = lineage(record)
            if metadata["root_job_id"] == root_id:
                version = metadata["version"]
                if version in versions:
                    raise NotesServiceError("revision_history_incomplete")
                versions[version] = directory.name
                published.append((version, directory.name))
        for record in records:
            if record["job_id"] in seen:
                continue
            try:
                job = self._restore_journal(record)
            except (ValueError, TypeError, KeyError):
                raise NotesServiceError("revision_history_incomplete") from None
            view = self._view(job)
            if view["root_job_id"] == root_id:
                if view["version"] in versions:
                    raise NotesServiceError("revision_history_incomplete")
                versions[view["version"]] = view["job_id"]
        if not published:
            raise NotesServiceError("revision_history_incomplete")
        return max(published)[1], max(versions) + 1

    def revise(self, parent_job_id, request_id, expected_parent_fingerprint, instruction, *,
               allow_model_text=False):
        if allow_model_text is not True:
            raise NotesServiceError("model_text_permission_required")
        try:
            parent_id, job_id = str(UUID(str(parent_job_id))), str(UUID(str(request_id)))
        except (ValueError, TypeError):
            raise NotesServiceError("invalid_notes_request") from None
        if (not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 4000
                or not isinstance(expected_parent_fingerprint, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_parent_fingerprint)):
            raise NotesServiceError("invalid_notes_request")
        instruction = instruction.strip()
        with self._lock:
            if self._closed:
                raise NotesServiceError("notes_stopped")
            parent, value = self._read(parent_id)
            if _digest(parent) != expected_parent_fingerprint:
                raise NotesServiceError("revision_parent_changed")
            kind = parent.get("output_kind", "structured_notes")
            bundle = deepcopy(parent["source_bundle"])
            ref, segments, digest = _source(bundle, kind)
            source_id, request = "artifact:" + parent_id, parent["user_request"]
            if (job_id in self._jobs or (self.output_root / job_id).exists()
                    or self._store.load(job_id) is not None):
                existing = self._job(job_id)
                revision = existing.get("revision")
                if (revision is None or revision["parent_job_id"] != parent_id
                        or revision["parent_record_sha256"] != expected_parent_fingerprint
                        or revision["instruction"] != instruction):
                    raise NotesServiceError("notes_request_conflict")
                frozen = stored_skill(existing.get("skill"), kind)
                if existing["signature"] != _signature(source_id, digest, request, kind, frozen, revision):
                    raise NotesServiceError("notes_request_conflict")
                return self._view(existing)
            if any(job["view"]["state"] in ACTIVE for job in self._jobs.values()):
                raise NotesServiceError("notes_busy")
            if len(self._jobs) >= 32:
                raise NotesServiceError("notes_capacity")
            root = lineage(parent)["root_job_id"]
            head, version = self._family_inventory(root)
            if head != parent_id:
                raise NotesServiceError("revision_parent_superseded")
            if version > MAX_LINEAGE:
                raise NotesServiceError("notes_capacity")
            if len(_json(segments).encode("utf-8")) > 200_000:
                raise NotesServiceError("notes_input_too_large")
            if len(_json(value["candidate"]).encode("utf-8")) > 200_000:
                raise NotesServiceError("notes_input_too_large")
            revision = checked_revision({"root_job_id": root, "parent_job_id": parent_id,
                                         "parent_record_sha256": expected_parent_fingerprint,
                                         "version": version, "instruction": instruction})
            return self._enqueue(job_id, source_id, request, kind, bundle, ref, digest,
                                 load_skill(kind), revision, value["candidate"])

    def _run(self, job):
        try:
            output_kind = job["view"]["output_kind"]
            build, validate, render = _functions(output_kind)

            with self._lock:
                if job["cancel"].is_set():
                    self._sync(job, state="canceled")
                    return
                self._sync(job, state="running", stage="generating")
            ref, segments, digest = _source(job["bundle"], output_kind)
            skill = stored_skill(job["skill"], output_kind)
            provider = self._provider()
            if job.get("revision"):
                provider = AmendmentProvider(provider, job["previous_candidate"], job["request"],
                                             job["revision"]["instruction"])
            candidate = build(segments, job["request"], ref, digest,
                              SkillProvider(provider, skill))
            candidate = validate(candidate, segments, job["request"], ref, digest)
            markdown = render(candidate)
            with self._lock:
                if job["cancel"].is_set():
                    self._sync(job, state="canceled")
                    return
                self._sync(job, stage="saving")
                self._save(job, candidate, markdown)
                job["view"]["state"] = "available"
                try:
                    self._sync(job)
                except NotesServiceError:
                    pass  # The complete immutable candidate remains the durable publication fact.
        except Exception as exc:  # noqa: BLE001 -- provider errors never reach HTTP/logs verbatim
            code = getattr(exc, "code", None)
            allowed = {"api_key_missing", "request_timeout", "network_error", "http_error",
                       "unexpected_response", "notes_write_failed", "destination_exists",
                       "invalid_notes_input", "invalid_notes_reply", "notes_identity_mismatch",
                       "invalid_citation", "notes_candidate_mismatch", "invalid_provider",
                       "invalid_provider_reply", "request_tampered", "payload_tampered",
                       "source_ref_tampered", "missing_required_field", "invalid_step_order",
                       "time_mismatch", "invalid_source_ref", "invalid_draft", "request_failed",
                       "invalid_answer_reply", "answer_identity_mismatch",
                       "answer_candidate_mismatch", "notes_storage_unavailable"}
            with self._lock:
                changes = ({"state": "canceled"} if job["cancel"].is_set() else
                           {"state": "failed", "error_code": code if code in allowed else
                            "notes_validation_failed" if isinstance(exc, ValueError) else "notes_failed"})
                try:
                    self._sync(job, **changes)
                except NotesServiceError:
                    job["view"].update(state="failed", error_code="notes_storage_unavailable")

    def _save(self, job, candidate, markdown):
        if job.get("revision"):
            parent, _ = self._read(job["revision"]["parent_job_id"])
            self._check_parent(job["revision"], parent, job["view"]["source_id"],
                               _source(job["bundle"], job["view"]["output_kind"])[2],
                               job["request"], job["view"]["output_kind"])
        target = self.output_root / job["view"]["job_id"]
        if target.exists():
            raise NotesServiceError("destination_exists")
        record = {"schema_version": 1, "owner": self.owner, "signature": job["signature"],
                  "job": {**job["view"], "state": "available"}, "source_bundle": job["bundle"],
                  "user_request": job["request"], "candidate": candidate,
                  "model_text_authorized": True, "formal_task": False, "completed": False}
        record["output_kind"] = job["view"]["output_kind"]
        record["skill"] = job["skill"]
        if job.get("revision"):
            record["revision"] = job["revision"]
        contents = {"candidate.json": _json(record), OUTPUT_FILES[record["output_kind"]]: markdown}
        if any(len(value.encode("utf-8")) > MAX_FILE_BYTES for value in contents.values()):
            raise NotesServiceError("notes_write_failed")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".notes-", dir=target.parent) as staging:
                stage = Path(staging)
                hashes = {}
                for name, content in contents.items():
                    (stage / name).write_bytes(content.encode("utf-8"))
                    hashes[name] = hashlib.sha256(content.encode("utf-8")).hexdigest()
                (stage / "manifest.json").write_bytes(_json(hashes).encode("utf-8"))
                if target.exists():
                    raise NotesServiceError("destination_exists")
                stage.rename(target)
        except OSError:
            raise NotesServiceError("notes_write_failed") from None

    def _read(self, job_id, _seen=frozenset()):
        try:
            if str(job_id) in _seen or len(_seen) >= MAX_LINEAGE:
                raise ValueError()
            _seen = _seen | {str(job_id)}
            target = self.output_root / str(UUID(str(job_id)))
            if target.is_symlink() or target.is_junction() or target.resolve().parent != self.output_root:
                raise ValueError()
            journal = self._store.load(job_id)
            if journal and journal["view"].get("state") in {"cancel_requested", "canceled"}:
                raise ValueError()
            raw = {}
            def read(name):
                path = target / name
                if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError()
                return path.read_bytes()

            raw["candidate.json"] = read("candidate.json")
            raw["manifest.json"] = read("manifest.json")
            record = json.loads(raw["candidate.json"])
            if not isinstance(record, dict):
                raise TypeError()
            output_kind = record.get("output_kind", "structured_notes")
            if output_kind not in OUTPUT_FILES:
                raise ValueError()
            markdown_name = OUTPUT_FILES[output_kind]
            raw[markdown_name] = read(markdown_name)
            manifest = json.loads(raw["manifest.json"])
            if not isinstance(manifest, dict) or set(manifest) != {"candidate.json", markdown_name} or any(
                hashlib.sha256(raw[name]).hexdigest() != digest for name, digest in manifest.items()
            ):
                raise ValueError()
            if (type(record["schema_version"]) is not int or record["schema_version"] != 1
                    or record["owner"] != self.owner
                    or record["formal_task"] is not False or record["completed"] is not False
                    or record["model_text_authorized"] is not True
                    or record["job"]["job_id"] != str(job_id)
                    or record["job"]["state"] != "available"
                    or record["job"].get("output_kind", "structured_notes") != output_kind):
                raise ValueError()
            ref, segments, digest = _source(record["source_bundle"], output_kind)
            skill = stored_skill(record.get("skill"), output_kind)
            revision = checked_revision(record["revision"]) if "revision" in record else None
            signature = _signature(record["job"]["source_id"], digest,
                                   record["user_request"], output_kind, skill, revision)
            if signature != record["signature"] or (record["job"]["bvid"], record["job"]["page"]) != (
                ref["bvid"], ref["page"]
            ):
                raise ValueError()
            metadata = lineage(record)
            if any(_json(record["job"].get(key, expected)) != _json(expected)
                   for key, expected in metadata.items()):
                raise ValueError()
            if revision:
                parent, _ = self._read(revision["parent_job_id"], _seen)
                self._check_parent(revision, parent, record["job"]["source_id"], digest,
                                   record["user_request"], output_kind)
            _, validate, render = _functions(output_kind)
            candidate = validate(record["candidate"], segments, record["user_request"], ref, digest)
            markdown = render(candidate)
            if raw[markdown_name] != markdown.encode("utf-8"):
                raise ValueError()
            return record, {"candidate": candidate, "markdown": markdown,
                            "reading_markdown": render_reading(candidate),
                            "skill": skill.metadata() if skill else None,
                            "revision": {**metadata, "record_fingerprint": _digest(record)},
                            "formal_task": False, "completed": False}
        except (OSError, ValueError, TypeError, KeyError):
            raise NotesServiceError("notes_candidate_unavailable") from None

    def _job(self, job_id):
        identity = str(job_id)
        if identity not in self._jobs:
            if (self.output_root / identity).exists():
                record, _ = self._read(identity)
                self._jobs[identity] = {"view": record["job"], "signature": record["signature"],
                                        "skill": record.get("skill"), "revision": record.get("revision"),
                                        "cancel": threading.Event()}
            else:
                record = self._store.load(identity)
                if record is None:
                    raise NotesServiceError("notes_candidate_unavailable")
                self._jobs[identity] = self._restore_journal(record)
        return self._jobs[identity]

    def get(self, job_id):
        with self._lock:
            return self._view(self._job(job_id))

    def candidate(self, job_id):
        with self._lock:
            if self._job(job_id)["view"]["state"] != "available":
                raise NotesServiceError("notes_not_ready")
            return self._read(str(job_id))[1]

    def inspection_context(self, job_id, *, include_sections=False):
        """Validated immutable version and complete amendment lineage; no external I/O."""
        with self._lock:
            record, projection = self._read(str(job_id))
            kind = record.get("output_kind", "structured_notes")
            if kind not in ("structured_notes", "source_answer") or (
                include_sections and kind != "structured_notes"
            ):
                raise NotesServiceError("notes_check_kind_unsupported")
            amendments = []
            ancestor = record
            while ancestor.get("revision"):
                revision = ancestor["revision"]
                amendments.append({"version": revision["version"],
                                   "instruction": revision["instruction"]})
                ancestor, _ = self._read(revision["parent_job_id"])
            _, segments, _ = _source(record["source_bundle"])
            context = {"candidate_id": str(job_id),
                             "record_fingerprint": _digest(record),
                             "root_job_id": projection["revision"]["root_job_id"],
                             "version": projection["revision"]["version"],
                             "user_request": record["user_request"],
                             "amendments": list(reversed(amendments)),
                             "candidate_text": projection["reading_markdown"],
                             "segments": segments}
            if kind == "source_answer":
                answer = projection["candidate"]
                # Keep the question in user_request only: a question repeated as
                # the reading title is not evidence that the answer covers it.
                context["candidate_text"] = "\n\n".join([
                    "回答正文", *(item["text"] for item in answer["answer"]),
                    "材料缺失", *answer["missing"],
                    "待核实", *answer["unverified"],
                ])
                context["output_kind"] = kind
            if include_sections:
                context["sections"] = projection["candidate"]["sections"]
            return deepcopy(context)

    def matches_request(self, job_id, source_id, user_request, output_kind):
        """Read-only binding check for a coordinator referencing an existing child."""
        with self._lock:
            job = self._job(job_id)
            request = job.get("journal_request")
            if request is None:
                record, _ = self._read(str(job_id))
                request = {"user_request": record["user_request"]}
            return (job["view"]["source_id"] == source_id
                    and request["user_request"] == user_request
                    and job["view"].get("output_kind", "structured_notes") == output_kind)

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
                    except NotesServiceError:
                        pass
                    job["cancel"].set()
                    job["view"]["state"] = "cancel_requested"
