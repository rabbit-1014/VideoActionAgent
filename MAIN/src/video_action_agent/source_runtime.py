"""Deterministic, version-fenced local-browser source-read admission and receipts."""

from dataclasses import asdict
from uuid import uuid4

from sqlalchemy import text

from .runtime import Conflict, encode
from .source_access import ADAPTER_VERSION, MAX_REPLY_BYTES, VideoRef, parse_reply, payload_hash


class SourceRuntime:
    def __init__(self, runtime, allowed_owner, browser_authorized):
        self.runtime = runtime
        self.allowed_owner = allowed_owner
        self.browser_authorized = browser_authorized

    def _scope(self, conn, claim, require_current=True):
        work, current = self.runtime._locked_claim(conn, claim)
        row = (
            conn.execute(
                text("""
            SELECT t.owner_id,c.input FROM tasks t JOIN task_contracts c
            ON c.task_id=t.task_id AND c.version=:version WHERE t.task_id=:task
        """),
                {"task": claim.task_id, "version": claim.contract_version},
            )
            .mappings()
            .one()
        )
        if (
            not self.browser_authorized
            or row["owner_id"] != self.allowed_owner
            or claim.mode != "execute"
            or work["kind"] != "form_goal"
            or (require_current and not current)
        ):
            raise Conflict("Source read is not authorized for this task/lease")
        return VideoRef.parse(row["input"].get("source")), current

    def admit(self, claim, action):
        if action not in {"video", "subtitle"}:
            raise Conflict("Unregistered source action")
        with self.runtime.engine.begin() as conn:
            ref, _ = self._scope(conn, claim)
            old = conn.execute(
                text(
                    "SELECT operation_id FROM source_reads"
                    " WHERE work_id=:work AND action=:action AND NOT retired_pre_dispatch"
                ),
                {"work": claim.work_id, "action": action},
            ).scalar_one_or_none()
            if old:
                return old
            operation_id = uuid4()
            source = asdict(ref)
            proposal = {
                "schema_version": 1,
                "purpose": "inspect_submitted_source",
                "source": source,
                "action": action,
                "adapter": ADAPTER_VERSION,
            }
            permission = {
                "schema_version": 1,
                "disposition": "auto_allow",
                "effect": "read_only",
                "source": source,
                "scope": "explicit_local_browser_owner",
                "policy": "local-source-read-v1",
                "contract_version": claim.contract_version,
            }
            conn.execute(
                text("""
                INSERT INTO source_reads
                (operation_id,proposal_id,permission_id,attempt_id,admission_id,task_id,
                 contract_version,revision,work_id,lease_token,action,adapter_version,
                 proposal,permission,phase,outcome)
                VALUES (:op,:proposal_id,:permission_id,:attempt,:admission,:task,:version,
                        :revision,:work,:token,:action,:adapter,CAST(:proposal AS jsonb),
                        CAST(:permission AS jsonb),'prepared','none')
            """),
                {
                    "op": operation_id,
                    "proposal_id": uuid4(),
                    "permission_id": uuid4(),
                    "attempt": uuid4(),
                    "admission": uuid4(),
                    "task": claim.task_id,
                    "version": claim.contract_version,
                    "revision": claim.revision,
                    "work": claim.work_id,
                    "token": claim.token,
                    "action": action,
                    "adapter": ADAPTER_VERSION,
                    "proposal": encode(proposal),
                    "permission": encode(permission),
                },
            )
            return operation_id

    def _operation(self, conn, claim, operation_id):
        row = (
            conn.execute(
                text("SELECT * FROM source_reads WHERE operation_id=:op FOR UPDATE"),
                {"op": operation_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or row["lease_token"] != claim.token
            or row["task_id"] != claim.task_id
            or row["work_id"] != claim.work_id
            or row["adapter_version"] != ADAPTER_VERSION
        ):
            raise Conflict("Source operation identity mismatch")
        if row["retired_pre_dispatch"]:
            # Retired pre-dispatch history is final: late writes never reopen it.
            raise Conflict("Retired source operation cannot be reused")
        return row

    def begin(self, claim, operation_id):
        with self.runtime.engine.begin() as conn:
            ref, _ = self._scope(conn, claim)
            row = self._operation(conn, claim, operation_id)
            if row["phase"] != "prepared":
                return None
            if row["proposal"]["source"] != asdict(ref):
                raise Conflict("Source scope changed")
            conn.execute(
                text(
                    "UPDATE source_reads SET phase='dispatching',"
                    "dispatched_at=clock_timestamp() WHERE operation_id=:op"
                ),
                {"op": operation_id},
            )
            return ref

    def finish(self, claim, operation_id, raw):
        return self._close(claim, operation_id, raw, None)

    def fail(self, claim, operation_id, code, raw=None):
        if code not in {
            "navigation_rejected",
            "adapter_failed",
            "adapter_timeout",
            "adapter_unavailable",
            "invalid_reply",
            "source_unavailable",
            "subtitle_not_returned",
            "browser_connection_failed",
            "source_auth_required",
            "source_mismatch",
            "reply_too_large",
        }:
            raise ValueError("Unknown diagnostic code")
        if raw is not None and len(raw) > MAX_REPLY_BYTES:
            raise ValueError("Rejected reply exceeds storage boundary")
        return self._close(claim, operation_id, raw, code)

    def _close(self, claim, operation_id, raw, code):
        with self.runtime.engine.begin() as conn:
            _, current = self.runtime._locked_claim(conn, claim)
            row = self._operation(conn, claim, operation_id)
            fingerprint = payload_hash(raw) if raw is not None else None
            if row["phase"] == "closed":
                if row["payload_hash"] != fingerprint or row["error_code"] != code:
                    raise Conflict("Immutable source receipt changed")
                return row["disposition"]
            if row["phase"] != "dispatching":
                raise Conflict("Source operation has not been dispatched")
            material = (
                parse_reply(row["action"], VideoRef(**row["proposal"]["source"]), raw)
                if raw is not None and code is None
                else None
            )
            disposition = "recorded" if current else "stale"
            outcome = (
                "succeeded"
                if material is not None
                else ("outcome_unknown" if code == "adapter_timeout" else "failed")
            )
            conn.execute(
                text("""
                UPDATE source_reads SET phase='closed',outcome=:outcome,disposition=:disposition,
                    error_code=:code,raw_payload=:raw,payload_hash=:hash,
                    material=CAST(:material AS jsonb),received_at=clock_timestamp()
                WHERE operation_id=:op
            """),
                {
                    "op": operation_id,
                    "outcome": outcome,
                    "disposition": disposition,
                    "code": code,
                    "raw": raw,
                    "hash": fingerprint,
                    "material": encode(material) if material is not None else None,
                },
            )
            return disposition

    def current(self, owner, task_id):
        with self.runtime.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text("""
                SELECT r.operation_id,r.action,r.material,r.payload_hash,r.received_at
                FROM source_reads r JOIN tasks t ON t.task_id=r.task_id
                JOIN task_contracts c ON c.task_id=t.task_id AND c.version=t.current_version
                WHERE t.task_id=:task AND t.owner_id=:owner AND c.state='forming'
                  AND r.contract_version=t.current_version AND r.disposition='recorded'
                  AND r.outcome='succeeded'
                ORDER BY r.created_at
            """),
                    {"task": task_id, "owner": owner},
                ).mappings()
            ]
