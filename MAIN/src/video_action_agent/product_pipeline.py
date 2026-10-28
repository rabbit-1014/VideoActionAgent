"""Explicit local task runner for the scoped Bilibili method-list path.

The default background Worker remains empty. This runner only advances a named
task belonging to a named owner and uses the task's declared subtitle scope.
"""

import json
import time
from pathlib import Path

from sqlalchemy import text

from .artifact_builder import ArtifactBuildError, build_method_list
from .audio_session import AudioSessionError
from .browser_session import SessionProbeError
from .deepseek_provider import ProviderError
from .formation_handler import build_formation_command
from .local_transcriber import TranscriptionError, transcribe_local
from .runtime import Conflict
from .source_access import (
    ADAPTER_VERSION,
    SOURCE_READ_FAILURE_CODES,
    SourceAccessError,
    VideoRef,
    parse_reply,
)
from .transcribed_source import (
    ASR_ADAPTER_VERSION,
    ASR_PARSE_VERSION,
    material_source_ref,
    parse_transcribed_reply,
)
from .zhipu_provider import ZhipuProviderError


class ProductPipeline:
    def __init__(self, runtime, owner, adapter, session_probe, draft_provider, *,
                 audio_session_factory=None, transcriber=transcribe_local,
                 audio_output_root=Path("D:/VideoActionAgent-asr-pilot/product-audio")):
        if not owner:
            raise ValueError("Task owner required")
        self.runtime = runtime
        self.owner = owner
        self.adapter = adapter
        self.session_probe = session_probe
        self.draft_provider = draft_provider
        self.audio_session_factory = audio_session_factory
        self.transcriber = transcriber
        self.audio_output_root = Path(audio_output_root)

    def run_once(self, task_id):
        """Advance at most one stage. Return a concise, non-sensitive status."""
        claim = self.runtime.claim(
            "product-goal", {"form_goal"}, allowed_owner=self.owner, task_id=task_id
        )
        if claim is not None:
            result = self.runtime.form_decision(
                claim, self.owner, build_formation_command(claim)
            )
            return {"stage": "formation", "decision": result["decision"]}

        claim = self.runtime.claim(
            "product-artifact", {"build_artifact"},
            allowed_owner=self.owner, task_id=task_id,
        )
        if claim is None:
            claim = self.runtime.claim(
                "product-artifact-recovery", {"build_artifact"},
                recover_expired=True, allowed_owner=self.owner, task_id=task_id,
            )
            if claim is not None and claim.mode == "reconcile":
                self.runtime.recover_artifact(claim, self.owner)
                return {"stage": "artifact", "state": "recovered_for_retry"}
        if claim is not None:
            materials = self.runtime.read_formal_materials(self.owner, task_id)
            material = next(
                (item for item in materials if item["successor_work_id"] == claim.work_id),
                None,
            )
            if material is None:
                raise Conflict("No current formal material for artifact work")
            try:
                draft = build_method_list(
                    material["segments"], claim.request["message"],
                    material_source_ref(material),
                    material["payload_hash"], self.draft_provider,
                )
            except (ProviderError, ZhipuProviderError, ArtifactBuildError) as exc:
                released = self.runtime.release_artifact_claim(
                    claim, self.owner, exc.code
                )
                return {"stage": "artifact", "state": "retryable_failure",
                        "code": released["error_code"]}
            saved = self.runtime.save_artifact_candidate(claim, self.owner, draft)
            return {"stage": "artifact", "state": saved["state"],
                    "artifact_id": saved["artifact_id"]}

        # A failed browser preflight must not reserve or consume read_source work.
        view = self.runtime.get(self.owner, task_id)
        if view["contract_state"] != "accepted" or view["work_kind"] != "read_source":
            return {"stage": "idle", "reason": "no_claimable_work"}
        with self.runtime.engine.connect() as conn:
            request = conn.execute(text("""
                SELECT c.input FROM tasks t JOIN task_contracts c
                  ON c.task_id=t.task_id AND c.version=t.current_version
                WHERE t.task_id=:task AND t.owner_id=:owner
            """), {"task": task_id, "owner": self.owner}).scalar_one()
        if request.get("source_mode") == "audio_transcription":
            if self.audio_session_factory is None:
                return {"stage": "source", "state": "waiting_for_local_asr",
                        "code": "explicit_audio_runner_required"}
            return self._run_audio_source(task_id)
        try:
            proof = self.session_probe.observe()
        except SessionProbeError as exc:
            return {"stage": "source", "state": "waiting_for_browser", "code": exc.code}
        claim = self.runtime.claim(
            "product-source", {"read_source"}, allowed_owner=self.owner, task_id=task_id
        )
        if claim is None:
            claim = self.runtime.claim(
                "product-source-recovery", {"read_source"}, recover_expired=True,
                allowed_owner=self.owner, task_id=task_id,
            )
        if claim is None:
            return {"stage": "idle", "reason": "source_claim_changed"}
        if claim.mode == "reconcile":
            recovered = self.runtime.recover_source_read(claim, self.owner)
            if recovered.action != "attempt_recovered":
                return {"stage": "source", "state": recovered.action}
            claim = recovered.claim
            operation_id, attempt_id = recovered.operation_id, recovered.attempt_id
            ref = VideoRef.parse(claim.request["source"])
        else:
            admission = self.runtime.prepare_source_read(claim, self.owner, "subtitle")
            claim = admission.claim
            operation_id = admission.operation_id
            ref = VideoRef(admission.bvid, admission.page)
            with self.runtime.engine.connect() as conn:
                attempt_id = conn.execute(
                    text("""
                    SELECT attempt_id FROM source_operation_attempts
                    WHERE operation_id=:op AND state='prepared'
                    ORDER BY attempt_seq DESC LIMIT 1
                    """),
                    {"op": operation_id},
                ).scalar_one()
        ticket = self.runtime.admit_dispatch(
            claim, self.owner, operation_id, attempt_id, proof
        )
        if not ticket.sendable:
            return {"stage": "source", "state": "dispatch_refused"}
        if not self.runtime.consume_dispatch(claim, self.owner, ticket):
            raise Conflict("Dispatch was already consumed")
        try:
            raw = self.adapter.fetch("subtitle", ref)
        except SourceAccessError as exc:
            code = str(exc)
            if code in SOURCE_READ_FAILURE_CODES:
                closed = self.runtime.settle_source_failure(
                    claim, self.owner, operation_id, code,
                    "The local source adapter explicitly rejected this read path; "
                    "this does not prove the video lacks subtitles or speech",
                )
            else:
                # Only stable unknown codes are recorded, never arbitrary exception text.
                code = "adapter_timeout" if code == "adapter_timeout" else "adapter_failed"
                closed = self.runtime.settle_source_unknown(
                    claim, self.owner,
                    {"schema_version": 1, "operation_id": str(operation_id),
                     "code": code,
                     "evidence": "The local adapter did not establish an external result "
                                 "after the one-shot dispatch was consumed",
                     "remaining_legal_paths": []},
                )
            return {"stage": "source", "state": closed.outcome, "code": code}
        try:
            parse_reply("subtitle", ref, raw)
        except SourceAccessError as exc:
            closed = self.runtime.settle_source_failure(
                claim, self.owner, operation_id, exc.args[0],
                "OpenCLI returned bytes, but the pinned subtitle parser rejected the reply",
            )
            return {"stage": "source", "state": closed.outcome, "code": exc.args[0]}
        settled = self.runtime.settle_source_result(
            claim, self.owner,
            {"schema_version": 1, "operation_id": str(operation_id),
             "action": "subtitle", "bvid": ref.bvid, "page": ref.page,
             "raw_reply": raw.decode("utf-8"), "adapter_version": ADAPTER_VERSION,
             "parse_version": "parse-v0"},
        )
        return {"stage": "source", "state": settled.disposition,
                "segments": settled.segments}

    def _run_audio_source(self, task_id):
        claim = self.runtime.claim("product-audio", {"read_source"},
                                   allowed_owner=self.owner, task_id=task_id)
        if claim is None:
            claim = self.runtime.claim("product-audio-recovery", {"read_source"},
                                       allowed_owner=self.owner, task_id=task_id, recover_expired=True)
        if claim is None:
            return {"stage": "idle", "reason": "source_claim_changed"}
        if claim.mode == "reconcile":
            recovered = self.runtime.recover_source_read(claim, self.owner)
            if recovered.action != "attempt_recovered":
                return {"stage": "source", "state": recovered.action}
            claim = recovered.claim
            operation_id, attempt_id = recovered.operation_id, recovered.attempt_id
        else:
            admitted = self.runtime.prepare_source_read(claim, self.owner, "audio_transcription")
            claim, operation_id = admitted.claim, admitted.operation_id
            with self.runtime.engine.connect() as conn:
                attempt_id = conn.execute(text("""
                    SELECT attempt_id FROM source_operation_attempts
                    WHERE operation_id=:op AND state='prepared'
                """), {"op": operation_id}).scalar_one()
        ref = VideoRef.parse(claim.request["source"])
        last_renewal = 0.0

        def checkpoint(force=False):
            nonlocal last_renewal
            now = time.monotonic()
            interval = min(5.0, self.runtime.lease_seconds / 3)
            if force or now - last_renewal >= interval:
                if not self.runtime.renew(claim):
                    raise Conflict("Audio task was canceled, revised, or lost its lease")
                last_renewal = now

        checkpoint(True)
        session = self.audio_session_factory(ref, on_poll=checkpoint)
        dispatched = False
        try:
            proof = session.resolve()
            checkpoint(True)
            ticket = self.runtime.admit_dispatch(claim, self.owner, operation_id, attempt_id, proof)
            if not ticket.sendable:
                return {"stage": "source", "state": "dispatch_refused"}
            if not self.runtime.consume_dispatch(claim, self.owner, ticket):
                raise Conflict("Audio dispatch was already consumed")
            dispatched = True
            destination = self.audio_output_root / str(task_id) / f"{operation_id}.m4a"
            downloaded = session.download(destination)
            checkpoint(True)
            transcript = self.transcriber(destination, on_poll=checkpoint, max_duration=7200,
                                          timeout_seconds=900)
            checkpoint(True)
            if transcript["audio_sha256"] != downloaded["audio_sha256"]:
                raise TranscriptionError("input_changed")
            raw = json.dumps({"schema_version": 1, "source_evidence": proof,
                              "transcription": transcript}, ensure_ascii=False, allow_nan=False)
            parse_transcribed_reply(ref, raw, proof)
            result = self.runtime.settle_source_result(claim, self.owner, {
                "operation_id": str(operation_id), "action": "audio_transcription",
                "bvid": ref.bvid, "page": ref.page, "raw_reply": raw,
                "adapter_version": ASR_ADAPTER_VERSION, "parse_version": ASR_PARSE_VERSION,
            })
            return {"stage": "source", "state": result.disposition, "segments": result.segments}
        except (AudioSessionError, TranscriptionError, SourceAccessError) as exc:
            checkpoint(True)
            code = getattr(exc, "code", str(exc))
            if isinstance(exc, AudioSessionError) and dispatched and code in {
                "timeout", "worker_closed", "download_protocol_failed", "download_failed",
            }:
                closed = self.runtime.settle_source_unknown(claim, self.owner, {
                    "operation_id": str(operation_id), "code": "adapter_failed",
                    "evidence": "Consumed audio dispatch did not establish a complete download result",
                    "remaining_legal_paths": [],
                })
            else:
                closed = self.runtime.settle_source_failure(
                    claim, self.owner, operation_id, code,
                    "The explicit audio or local ASR path failed; no claim of absent speech or content",
                )
            return {"stage": "source", "state": closed.outcome, "code": code}
        finally:
            session.close()

    def run_until_idle(self, task_id, max_steps=3):
        last = None
        for _ in range(max_steps):
            last = self.run_once(task_id)
            if last["stage"] == "idle" or last.get("state") in {
                "waiting_for_browser", "waiting_for_local_asr", "dispatch_refused", "outcome_unknown", "failed",
                "unreviewed_candidate", "retryable_failure", "requires_source_reconciliation",
                "version_no_longer_current",
            } or last.get("decision") != "accept" and last["stage"] == "formation":
                break
        return last
