"""Native note adapter for existing LocalFlow; no parallel flow orchestrator."""
import hashlib
import json
import re
import threading
import time
import traceback
import types
from pathlib import Path
from urllib.error import HTTPError
from uuid import UUID

from bridge_contract import BridgeError, BridgeLedger, atomic, digest
from video_action_agent.content_intake import IntakeError
from video_action_agent.notes_service import NotesServiceError
from video_action_agent.source_candidate_bundle import CandidateBundleError
from video_action_agent.zhipu_provider import ZhipuProviderError

STATE_MAP = {"admitted":"queued", "model_dispatched":"running", "reply_received":"running",
             "available":"available", "cancelled":"canceled", "interrupted":"interrupted",
             "model_failed_or_unknown":"interrupted"}
ACTIVE = {"queued", "running"}
_SAFE_PROVIDER_CODES = frozenset({
    "model_not_configured", "invalid_timeout", "invalid_transport",
    "invalid_reasoning_effort", "api_key_missing", "request_timeout",
    "http_error", "network_error", "unexpected_response",
})


def _transport_failure_diagnostic(exc, elapsed_seconds):
    """Build a diagnostic from bounded metadata only; never inspect error text."""
    category = type(exc).__name__
    if not isinstance(category, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", category):
        category = "unknown_exception"
    diagnostic = {"exception_type": category, "elapsed_seconds": elapsed_seconds}
    if isinstance(exc, ZhipuProviderError) and exc.code in _SAFE_PROVIDER_CODES:
        diagnostic["provider_code"] = exc.code
    cause = exc.__cause__
    if isinstance(cause, HTTPError) and type(cause.code) is int:
        diagnostic["http_status"] = cause.code
    return diagnostic


class CaptureLedger(BridgeLedger):
    def __init__(self, root, sources, owner):
        super().__init__(root, {})
        self.sources, self.owner = sources, owner

    def _master(self, key):
        try:
            identity = str(UUID(str(key)))
            bundle = self.sources.bundle(identity)
            return bundle["source_envelope"], digest(bundle)
        except (ValueError, TypeError, KeyError, IntakeError, CandidateBundleError):
            raise BridgeError("source_bundle_unavailable") from None

    def admit(self, *args, **kwargs):
        with self.lock:
            row, fresh = super().admit(*args, **kwargs)
            if fresh:
                row = self.update(row["request_id"], owner=self.owner,
                                  fingerprint_kind="validated_bundle_canonical_json")
            elif row.get("owner") != self.owner:
                raise BridgeError("owned_request_not_found")
            return row, fresh

    def get(self, identity):
        row = super().get(identity)
        if row.get("owner") not in (None, self.owner):
            raise BridgeError("owned_request_not_found")
        return row


class NativeClient:
    def __init__(self, service, identity):
        self.service, self.identity = service, identity
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    def create(self, *, model, messages, **kwargs):
        ledger, output = self.service.ledger, self.service.native_root
        configure = getattr(self.service, 'request_configuration', None)
        configuration = (configure(self.identity) if callable(configure) else
            {'model':'glm-5.3','reasoning_effort':'high','stream':True,'direct':True})
        if (configuration.get('model') not in {'glm-5.3','deepseek-v4-pro','gpt-6.1-sol'}
                or model != configuration['model'] or len(messages)!=1 or messages[0].get("role")!="user"
                or not isinstance(messages[0].get("content"),str)):
            raise BridgeError("native_request_not_admitted")
        ledger.material(self.identity)
        atomic(output/(self.identity+".model-request.json"), {
            **configuration,
            "native_commit":"90940fde4d224d0f69ad99674f4af0a1ea52d3c4",
            "extras_version":"purpose_visual_20261015_v1", "messages":messages,
            "native_temperature_not_forwarded":kwargs.get("temperature")})
        ledger.dispatch(self.identity)
        started=time.monotonic()
        try:
            scoped=getattr(self.service,"request_transport",None)
            text=(scoped(self.identity,messages[0]["content"]) if callable(scoped)
                  else self.service.transport(messages[0]["content"]))
            if not isinstance(text,str) or not text.strip():
                raise BridgeError("empty_model_reply")
        except Exception as exc:  # noqa: BLE001 -- retain unknown and redact external error text; no retry
            try:
                atomic(output/(self.identity+".transport-failure.json"),
                       _transport_failure_diagnostic(exc, time.monotonic()-started))
            except OSError:
                pass  # diagnostics must never replace the existing unknown-failure path
            ledger.update(self.identity,state="model_failed_or_unknown",error_code="model_failed_or_unknown")
            raise BridgeError("model_failed_or_unknown") from None
        (output/(self.identity+".raw-model.md")).write_text(text,encoding="utf-8")
        ledger.update(self.identity,state="reply_received",model_seconds=time.monotonic()-started,
                      reply_received=True)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))])


class NativeNotes:
    def __init__(self, owner, output_root, sources, generate, transport, *, result_guard=None):
        if not owner or owner!=sources.owner:
            raise ValueError("Same owner and source service required")
        path=Path(output_root)
        if path.is_symlink() or path.is_junction() or str(path).startswith(("\\\\","//")):
            raise ValueError("Local output root required")
        self.owner, self._capture = owner, sources
        self.output_root=path.resolve()
        self.output_root.mkdir(parents=True,exist_ok=True)
        self.native_root=self.output_root/"native"
        self.native_root.mkdir(exist_ok=True)
        self.ledger=CaptureLedger(self.output_root/".native-jobs",sources,owner)
        self.generate, self.transport=generate, transport
        self.result_guard=result_guard
        self._lock=threading.RLock()
        self._threads={}
        self._closed=False

    def _row(self, identity):
        try:
            row=self.ledger.get(identity)
            if row.get("owner")!=self.owner:
                raise BridgeError("owned_request_not_found")
            return row
        except (BridgeError,OSError,ValueError):
            raise NotesServiceError("notes_not_found") from None

    def _result(self, row):
        if row["state"]!="available":
            raise NotesServiceError("notes_not_ready")
        try:
            self.ledger.material(row["request_id"])
            raw=(self.native_root/(row["request_id"]+".json")).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=row["native_result_sha256"]:
                raise BridgeError("native_result_changed")
            if self.result_guard is not None:
                self.result_guard(row)
            result=json.loads(raw)
            if not isinstance(result["markdown"],str) or not result["markdown"].strip():
                raise BridgeError("native_result_invalid")
            return result
        except (BridgeError,OSError,ValueError,KeyError):
            raise NotesServiceError("notes_candidate_unavailable") from None

    def get(self, identity):
        with self._lock:
            row=self._row(identity)
            status=STATE_MAP.get(row["state"],"failed")
            restored=status in ACTIVE and (identity not in self._threads or not self._threads[identity].is_alive())
            if restored:
                status="interrupted"
            ref=row["source_url"].split("/video/",1)[1]
            bvid,page=ref.split("?p=")
            if status=="available":
                self._result(row)
            return {"job_id":row["request_id"],"request_id":row["request_id"],"master_id":row["master_id"],
                    "bvid":bvid,"page":int(page),"user_request":row["user_request"],"state":status,
                    "error_code":("previous_run_unconfirmed" if restored else row.get("error_code")),
                    "output_kind":"structured_notes","source_url":row["source_url"],
                    "created_at":row.get("created_at",""),"model_calls":row["model_calls"],
                    "formal_task":False,"completed":False,"quality_approved":False}

    def history(self):
        rows=[self.get(p.stem) for p in self.ledger.root.glob("*.json")]
        return {"jobs":rows}

    def current(self):
        with self._lock:
            rows=self.history()["jobs"]
            active=next((r for r in rows if r["state"] in ACTIVE),None)
            return {"enabled":not self._closed,"job":active or (rows[-1] if rows else None)}

    def start(self, request_id, source_id, user_request, output_kind="structured_notes", *,allow_model_text=False,
              allow_local_edits=False):
        if output_kind!="structured_notes":
            raise NotesServiceError("unsupported_output_kind")
        with self._lock:
            if self._closed:
                raise NotesServiceError("notes_stopped")
            try:
                row,fresh=self.ledger.admit(request_id,source_id,user_request,allow_model_text=allow_model_text,
                                            allow_local_edits=allow_local_edits)
            except BridgeError as exc:
                raise NotesServiceError(str(exc)) from None
            identity=row["request_id"]
            if fresh:
                thread=threading.Thread(target=self._run,args=(identity,),daemon=True)
                self._threads[identity]=thread
                try:
                    thread.start()
                except RuntimeError:
                    self.ledger.update(identity,state="interrupted",error_code="native_start_failed")
            return self.get(identity)

    def _run(self, identity):
        try:
            row=self.ledger.get(identity)
            material=self.ledger.material(identity)
            path=self.generate(identity,material,row["user_request"],row["style"],NativeClient(self,identity),self.native_root)
            expected=self.native_root/(identity+".json")
            if Path(path)!=expected:
                raise BridgeError("native_result_identity_mismatch")
            raw=expected.read_bytes()
            result=json.loads(raw)
            if not isinstance(result.get("markdown"),str) or not result["markdown"].strip():
                raise BridgeError("native_result_invalid")
            self.ledger.publish(identity,native_result_sha256=hashlib.sha256(raw).hexdigest(),
                                markdown_sha256=hashlib.sha256(result["markdown"].encode()).hexdigest())
        except Exception as exc:  # noqa: BLE001 -- native library boundary; no replay or raw diagnostics in HTTP
            try:
                atomic(self.native_root/(identity+".failure.json"), {
                    "error_type":type(exc).__name__,
                    "frames":[{"file":item.filename,"line":item.lineno,"function":item.name}
                              for item in traceback.extract_tb(exc.__traceback__)]})
            except OSError:
                pass  # Diagnostics must not replace the original failure state.
            row=self.ledger.get(identity)
            if row["state"] not in {"cancelled","model_failed_or_unknown"}:
                code=str(exc) if isinstance(exc,BridgeError) else "native_failed_or_unconfirmed"
                self.ledger.update(identity,state="native_failed_or_unconfirmed",error_code=code)

    def candidate(self, identity):
        with self._lock:
            row=self._row(identity)
            return {**self.get(identity),"result":self._result(row)}

    def matches_request(self, identity, source_id, user_request, output_kind):
        row=self._row(identity)
        return (row["master_id"],row["user_request"],"structured_notes")== (source_id,user_request,output_kind)

    def cancel(self, identity):
        with self._lock:
            row=self._row(identity)
            if STATE_MAP.get(row["state"],"failed") in ACTIVE:
                self.ledger.cancel(identity)
            return self.get(identity)

    def wait(self, identity, timeout=.1):
        thread=self._threads.get(identity)
        if thread is not None and thread.ident is not None:
            thread.join(timeout)

    def close(self):
        with self._lock:
            self._closed=True
            for identity,thread in self._threads.items():
                if thread.is_alive():
                    self.cancel(identity)
        for thread in self._threads.values():
            if thread.ident is not None:
                thread.join(2)
