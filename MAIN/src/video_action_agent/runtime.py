"""Deterministic I1 lifecycle writer. Queue receipts are not product results."""

import json
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import text

from .models import (
    DispatchAudioProof,
    DispatchSessionProof,
    FormationCommand,
    SourceMaterialSegment,
    SourceSettlementInput,
    SourceUnknownInput,
    TaskInput,
)
from .source_access import (
    ADAPTER_VERSION,
    SourceAccessError,
    VideoRef,
    parse_reply,
    payload_hash,
)
from .transcribed_source import (
    ASR_ADAPTER_VERSION,
    ASR_PARSE_VERSION,
    ASR_POLICY,
    material_source_ref,
    parse_transcribed_reply,
    source_scope,
)


def source_policy(action):
    return ASR_POLICY if action == "audio_transcription" else SOURCE_READ_POLICY


def source_adapter_version(action):
    return ASR_ADAPTER_VERSION if action == "audio_transcription" else ADAPTER_VERSION


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def digest(value):
    return sha256(encode(value).encode()).hexdigest()


def task_input_payload(request):
    payload = TaskInput.model_validate(request).model_dump()
    # Preserve the persisted input and idempotency hash of requests admitted
    # before local browser scope was added. An omitted scope means no access.
    if not payload["local_browser_actions"]:
        del payload["local_browser_actions"]
    if payload["source_mode"] == "subtitle":
        del payload["source_mode"]
    if not payload["local_source_actions"]:
        del payload["local_source_actions"]
    if payload["output_kind"] is None:
        del payload["output_kind"]
    return payload


# Registered work kinds and the only contract state each may be claimed in.
KIND_CONTRACT_STATES = {
    "form_goal": "forming",
    "read_source": "accepted",
    "build_artifact": "accepted",
}
# Only kinds with a resolver may be taken over after a lease expires.
RECOVERY_KINDS = ("form_goal", "read_source", "build_artifact")
# Kinds whose handler is registered in this process. ``build_artifact`` is absent
# until its real handler exists, so the default worker can never claim it.
HANDLER_REGISTERED_KINDS = frozenset({"form_goal", "read_source", "build_artifact"})
# Fixed policy terms of the deterministic read-only source admission.
SOURCE_READ_POLICY = "accepted-local-bilibili-read-v1"
SOURCE_READ_PURPOSE = "read the declared bilibili video for the accepted required goals"
SOURCE_READ_STOP = "stop on any unauthorized, unavailable or out-of-scope response"
SOURCE_READ_TIME_LIMIT = "one read inside a single lease; recheck authorization before dispatch"


@dataclass(frozen=True)
class SourceActionCandidate:
    """Read-only scope candidate for a future production source read.

    Pure data for the next responsibility area to re-validate. It is not a
    permission, carries no credentials or page content, and proves nothing about
    browser availability at the moment it is used.
    """

    schema_version: int
    task_id: UUID
    contract_version: int
    revision: int
    work_id: UUID
    action: str
    bvid: str
    page: int
    input_fingerprint: str
    entry_scope: tuple


@dataclass(frozen=True)
class SourceRecovery:
    """Outcome of same-identity recovery for a formal prepared source operation.

    ``action`` is ``attempt_recovered`` (the next attempt of the SAME operation
    opened), ``reset_to_ready`` (no source operation existed at all) or
    ``requires_source_reconciliation``/``version_no_longer_current`` (facts
    possibly dispatched or no longer current: nothing is retried).
    """

    schema_version: int
    action: str
    work_id: UUID
    operation_id: UUID | None
    attempt_id: UUID | None
    attempt_seq: int | None
    revision: int
    idempotent: bool
    claim: "Claim | None"


@dataclass(frozen=True)
class SourceAdmission:
    """Formal pre-dispatch admission identities plus the claim that owns them now.

    The admission stops at a prepared operation: it is not a permission to
    dispatch, and the browser connection state stays unverified.
    """

    schema_version: int
    proposal_id: UUID
    permission_decision_id: UUID
    operation_id: UUID
    action: str
    bvid: str
    page: int
    revision: int
    uniqueness_decision: str
    claim: "Claim"


@dataclass(frozen=True)
class Claim:
    task_id: UUID
    work_id: UUID
    token: UUID
    owner: str
    revision: int
    contract_version: int
    mode: str
    kind: str
    request: dict
    run_id: UUID
    trace_id: UUID


@dataclass(frozen=True)
class DispatchTicket:
    """One-shot right to send exactly one source request outside.

    Persisted before any external I/O. The executor must re-check every field
    here against live rows immediately before sending: possession of a ticket is
    not a substitute for that re-check.
    """

    schema_version: int
    admission_id: UUID
    operation_id: UUID
    attempt_id: UUID
    attempt_seq: int
    dispatch_generation: int
    task_id: UUID
    work_id: UUID
    contract_version: int
    revision: int
    owner: str
    action: str
    bvid: str
    page: int
    lease_token: UUID
    session_proof_kind: str
    sendable: bool


@dataclass(frozen=True)
class SourceSettlement:
    """Outcome of formal source result adjudication."""

    schema_version: int
    disposition: str
    material_id: UUID
    payload_hash: str
    segments: int
    successor_work_id: UUID | None
    expected_revision: int
    idempotent: bool


@dataclass(frozen=True)
class SourceUnknownClose:
    """Outcome of an honest unknown-outcome close."""

    schema_version: int
    disposition: str
    outcome: str
    closed_work: bool
    goals_indeterminate: int
    expected_revision: int


class Runtime:
    def __init__(self, engine, lease_seconds=60):
        if engine.dialect.name != "postgresql":
            raise ValueError("PostgreSQL is required")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self.engine = engine
        self.lease_seconds = lease_seconds

    def _view(self, conn, owner, task_id, lock=False):
        row = (
            conn.execute(
                text(
                    """
            SELECT t.task_id, t.owner_id, t.revision, t.current_version,
                   t.run_id, t.trace_id, c.state AS contract_state,
                   w.state AS work_state, w.kind AS work_kind,
                   COALESCE(w.is_current, false) AS work_current,
                   w.outcome AS last_work_outcome
            FROM tasks t
            JOIN task_contracts c ON c.task_id=t.task_id AND c.version=t.current_version
            LEFT JOIN LATERAL (
                SELECT wi.kind,wi.state,r.outcome,
                       COALESCE(wi.expected_revision=t.revision
                           AND (CAST(:kind_states AS jsonb) ->> wi.kind)=c.state,
                           false) AS is_current
                FROM work_items wi
                LEFT JOIN work_receipts r ON r.receipt_id=wi.receipt_id
                WHERE wi.task_id=t.task_id AND wi.contract_version=t.current_version
                ORDER BY is_current DESC, (wi.state <> 'closed') DESC,
                         (wi.expected_revision=t.revision) DESC,
                         wi.created_at DESC, wi.work_id
                LIMIT 1
            ) w ON true
            WHERE t.task_id=:task AND t.owner_id=:owner
            """
                    + (" FOR UPDATE OF t" if lock else "")
                ),
                {"task": task_id, "owner": owner, "kind_states": encode(KIND_CONTRACT_STATES)},
            )
            .mappings()
            .first()
        )
        if row is None:
            raise NotFound()
        completed = False
        if row["contract_state"] == "accepted" and conn.execute(
            text("SELECT to_regclass('artifact_candidates')")
        ).scalar_one() is not None:
            completion = conn.execute(
                text("""
            SELECT
              (SELECT count(*) FROM goals g WHERE g.task_id=:task
                 AND g.contract_version=:version AND g.required) AS required_total,
              (SELECT count(*) FROM goals g WHERE g.task_id=:task
                 AND g.contract_version=:version AND g.required
                 AND g.state='satisfied' AND g.progress='complete') AS satisfied_total,
              (SELECT count(*) FROM goal_blockers b WHERE b.task_id=:task
                 AND b.contract_version=:version AND b.state='open') AS open_blockers,
              EXISTS(SELECT 1 FROM transitions x JOIN artifact_candidates a
                  ON a.artifact_id=x.object_ref AND a.task_id=x.task_id
                  WHERE x.task_id=:task AND x.kind='artifact_owner_accepted'
                    AND a.contract_version=:version AND a.owner_id=:owner) AS owner_accepted
            """),
                {"task": task_id, "version": row["current_version"], "owner": owner},
            ).mappings().one()
            completed = bool(
                completion["required_total"] > 0
                and completion["required_total"] == completion["satisfied_total"]
                and completion["open_blockers"] == 0
                and completion["owner_accepted"]
            )
        return {
            **row,
            "formation": self._formation(conn, task_id, row["current_version"]),
            "completed": completed,
        }

    def _formation(self, conn, task_id, version):
        """Formation state comes only from formal decisions, never from free receipts."""
        decision = (
            conn.execute(
                text("""
            SELECT formation_id,decision,input_fingerprint,candidate_fingerprint
            FROM goal_formations WHERE task_id=:task AND contract_version=:version
            """),
                {"task": task_id, "version": version},
            )
            .mappings()
            .first()
        )
        if decision is None:
            return None
        goals = {
            True: [],
            False: [],
        }
        for row in (
            conn.execute(
                text("""
            SELECT goal_id,required,title,acceptance,basis,evidence,state,progress
            FROM goals WHERE task_id=:task AND contract_version=:version ORDER BY created_at,goal_id
            """),
                {"task": task_id, "version": version},
            )
            .mappings()
            .all()
        ):
            goals[row["required"]].append(dict(row))
        blocker = (
            conn.execute(
                text("""
            SELECT blocker_id,question,waiting_condition,responsible_party,advanceable,
                   exit_path,state
            FROM goal_blockers WHERE task_id=:task AND contract_version=:version
            ORDER BY created_at DESC,blocker_id LIMIT 1
            """),
                {"task": task_id, "version": version},
            )
            .mappings()
            .first()
        )
        return {
            "formation_id": str(decision["formation_id"]),
            "decision": decision["decision"],
            "input_fingerprint": decision["input_fingerprint"],
            "candidate_fingerprint": decision["candidate_fingerprint"],
            "required_goals": goals[True],
            "optional_goals": goals[False],
            "blocker": dict(blocker) if blocker is not None else None,
            "waiting": blocker is not None and blocker["state"] == "open",
        }

    def get(self, owner, task_id):
        with self.engine.connect() as conn:
            return self._view(conn, owner, task_id)

    def _enqueue(self, conn, task_id, version, revision, run_id):
        conn.execute(
            text("""
            INSERT INTO work_items
                (work_id,task_id,contract_version,expected_revision,run_id,kind,state)
            VALUES (:work,:task,:version,:revision,:run,'form_goal','ready')
            """),
            {
                "work": uuid4(),
                "task": task_id,
                "version": version,
                "revision": revision,
                "run": run_id,
            },
        )

    def _transition(
        self,
        conn,
        task_id,
        revision,
        kind,
        *,
        object_kind=None,
        object_ref=None,
        from_state=None,
        to_state=None,
        reason_ref=None,
        policy_version=None,
        dedup_key=None,
    ):
        conn.execute(
            text("""
            INSERT INTO transitions
            (task_id,revision,kind,object_kind,object_ref,from_state,to_state,reason_ref,
             policy_version,dedup_key)
            VALUES (:task,:revision,:kind,:object_kind,:object_ref,:from_state,:to_state,
                    :reason_ref,:policy_version,:dedup_key)
            """),
            {
                "task": task_id,
                "revision": revision,
                "kind": kind,
                "object_kind": object_kind,
                "object_ref": object_ref,
                "from_state": from_state,
                "to_state": to_state,
                "reason_ref": reason_ref,
                "policy_version": policy_version,
                "dedup_key": dedup_key,
            },
        )

    def submit(self, owner, key, request):
        request = task_input_payload(request)
        if not owner or not key or len(key) > 128:
            raise ValueError("owner and bounded idempotency key required")
        fingerprint = digest(request)
        task_id, run_id = uuid4(), uuid4()
        with self.engine.begin() as conn:
            inserted = conn.execute(
                text("""
                INSERT INTO tasks
                    (task_id,owner_id,idempotency_key,request_hash,current_version,revision,run_id,trace_id)
                VALUES (:task,:owner,:key,:hash,1,1,:run,:trace)
                ON CONFLICT (owner_id,idempotency_key) DO NOTHING RETURNING task_id
                """),
                {
                    "task": task_id,
                    "owner": owner,
                    "key": key,
                    "hash": fingerprint,
                    "run": run_id,
                    "trace": uuid4(),
                },
            ).scalar_one_or_none()
            if inserted is None:
                old = (
                    conn.execute(
                        text("""
                    SELECT task_id,request_hash FROM tasks
                    WHERE owner_id=:owner AND idempotency_key=:key
                    """),
                        {"owner": owner, "key": key},
                    )
                    .mappings()
                    .one()
                )
                if old["request_hash"] != fingerprint:
                    raise Conflict("Idempotency key already belongs to different input")
                return self._view(conn, owner, old["task_id"])
            conn.execute(
                text("""
                INSERT INTO task_contracts (task_id,version,state,input)
                VALUES (:task,1,'forming',CAST(:input AS jsonb))
                """),
                {"task": task_id, "input": encode(request)},
            )
            self._transition(conn, task_id, 1, "admitted")
            self._enqueue(conn, task_id, 1, 1, run_id)
            return self._view(conn, owner, task_id)

    def cancel(self, owner, task_id, expected_revision):
        with self.engine.begin() as conn:
            task = self._view(conn, owner, task_id, lock=True)
            if task["contract_state"] == "canceled" and expected_revision in (
                task["revision"],
                task["revision"] - 1,
            ):
                return task
            if task["revision"] != expected_revision:
                raise Conflict("Task changed; refresh before canceling")
            if task["contract_state"] == "unformable":
                # This contract version is already terminal. No cancel fact is created.
                return task
            if task["contract_state"] in {"forming", "accepted"}:
                conn.execute(
                    text("""
                    UPDATE task_contracts SET state='canceled'
                    WHERE task_id=:task AND version=:version
                    """),
                    {"task": task_id, "version": task["current_version"]},
                )
                self._end_goals(conn, task_id, task["current_version"], "canceled")
                self._end_blockers(conn, task_id, task["current_version"], "canceled")
            # A terminal 'unformable' version is never rewritten by a cancel.
            conn.execute(
                text("UPDATE tasks SET revision=revision+1 WHERE task_id=:task"), {"task": task_id}
            )
            conn.execute(
                text("""
                UPDATE work_items SET state='suppressed'
                WHERE task_id=:task AND state IN ('ready','leased')
                """),
                {"task": task_id},
            )
            self._transition(conn, task_id, task["revision"] + 1, "canceled")
            return self._view(conn, owner, task_id)

    def revise(self, owner, task_id, expected_revision, request):
        request = task_input_payload(request)
        with self.engine.begin() as conn:
            task = self._view(conn, owner, task_id, lock=True)
            if task["revision"] != expected_revision or task["contract_state"] not in {
                "forming",
                "accepted",
                "unformable",
            }:
                raise Conflict("Task cannot be revised at the supplied revision")
            version, revision = task["current_version"] + 1, task["revision"] + 1
            run_id = uuid4()
            if task["contract_state"] in {"forming", "accepted"}:
                conn.execute(
                    text("""
                    UPDATE task_contracts SET state='superseded'
                    WHERE task_id=:task AND version=:version
                    """),
                    {"task": task_id, "version": task["current_version"]},
                )
                self._end_goals(conn, task_id, task["current_version"], "superseded")
                self._end_blockers(conn, task_id, task["current_version"], "canceled")
            # A terminal 'unformable' version is kept exactly as recorded.
            conn.execute(
                text("""
                INSERT INTO task_contracts (task_id,version,state,input)
                VALUES (:task,:version,'forming',CAST(:input AS jsonb))
                """),
                {"task": task_id, "version": version, "input": encode(request)},
            )
            conn.execute(
                text("""
                UPDATE tasks SET current_version=:version,revision=:revision,run_id=:run
                WHERE task_id=:task
                """),
                {"task": task_id, "version": version, "revision": revision, "run": run_id},
            )
            conn.execute(
                text("""
                UPDATE work_items SET state='suppressed'
                WHERE task_id=:task AND state IN ('ready','leased')
                """),
                {"task": task_id},
            )
            self._transition(conn, task_id, revision, "input_revised")
            self._enqueue(conn, task_id, version, revision, run_id)
            return self._view(conn, owner, task_id)

    def claim(self, worker, kinds, recover_expired=False, allowed_owner=None, task_id=None):
        # A kind is claimable only once its real handler exists in this process.
        # Unknown kinds are ignored, matching the long-standing behaviour that an
        # unregistered kind simply yields no work: ``build_artifact`` therefore
        # stays queued and unclaimable until its handler is registered.
        kinds = [kind for kind in kinds if kind in HANDLER_REGISTERED_KINDS]
        if not kinds:
            return None
        if not worker:
            raise ValueError("worker identity required")
        if task_id is not None:
            task_id = UUID(str(task_id))
        with self.engine.begin() as conn:
            # Every mutation locks task before work; external calls never hold this lock.
            row = (
                conn.execute(
                    text("""
                SELECT t.task_id,t.trace_id,w.work_id,w.expected_revision,w.contract_version,
                       w.run_id,w.kind,w.state,c.input
                FROM tasks t JOIN work_items w ON t.task_id=w.task_id
                JOIN task_contracts c ON c.task_id=t.task_id AND c.version=t.current_version
                WHERE w.contract_version=t.current_version
                  AND c.state = (CAST(:kind_states AS jsonb) ->> w.kind)
                  AND (CAST(:owner AS text) IS NULL OR t.owner_id=:owner)
                  AND (CAST(:task AS uuid) IS NULL OR t.task_id=:task)
                  AND w.expected_revision=t.revision AND w.kind=ANY(:kinds)
                  AND w.available_at<=clock_timestamp()
                  AND (w.state='ready' OR
                      (:recover AND w.state='leased' AND w.lease_until<=clock_timestamp()
                       AND w.kind=ANY(:recoverable)))
                ORDER BY w.available_at,w.work_id
                LIMIT 1 FOR UPDATE OF t SKIP LOCKED
                """),
                    {
                        "kinds": sorted(kinds),
                        "recover": recover_expired,
                        "recoverable": sorted(RECOVERY_KINDS),
                        "owner": allowed_owner,
                        "task": task_id,
                        "kind_states": encode(KIND_CONTRACT_STATES),
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            # The first SELECT may have taken its snapshot before a previous claimant
            # committed. The task lock alone does not refresh the joined work row.
            row = (
                conn.execute(
                    text("""
                    SELECT t.task_id,t.trace_id,w.work_id,w.expected_revision,w.contract_version,
                           w.run_id,w.kind,w.state,c.input
                    FROM tasks t JOIN work_items w ON t.task_id=w.task_id
                    JOIN task_contracts c ON c.task_id=t.task_id AND c.version=t.current_version
                    WHERE w.work_id=:work
                      AND c.state = (CAST(:kind_states AS jsonb) ->> w.kind)
                      AND (CAST(:owner AS text) IS NULL OR t.owner_id=:owner)
                      AND (CAST(:task AS uuid) IS NULL OR t.task_id=:task)
                      AND w.contract_version=t.current_version AND w.expected_revision=t.revision
                      AND w.kind=ANY(:kinds) AND w.available_at<=clock_timestamp()
                      AND (w.state='ready' OR
                          (:recover AND w.state='leased' AND w.lease_until<=clock_timestamp()
                           AND w.kind=ANY(:recoverable)))
                    FOR UPDATE OF w
                    """),
                    {
                        "work": row["work_id"],
                        "kinds": sorted(kinds),
                        "recover": recover_expired,
                        "recoverable": sorted(RECOVERY_KINDS),
                        "owner": allowed_owner,
                        "task": task_id,
                        "kind_states": encode(KIND_CONTRACT_STATES),
                    },
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            token = uuid4()
            mode = "reconcile" if row["state"] == "leased" else "execute"
            conn.execute(
                text("""
                UPDATE work_items SET state='leased',lease_token=:token,lease_owner=:owner,
                    lease_until=clock_timestamp()+(:seconds * interval '1 second')
                WHERE work_id=:work
                """),
                {
                    "token": token,
                    "owner": worker,
                    "seconds": self.lease_seconds,
                    "work": row["work_id"],
                },
            )
            conn.execute(
                text("""
                INSERT INTO work_leases (token,work_id,owner,revision,mode)
                VALUES (:token,:work,:owner,:revision,:mode)
                """),
                {
                    "token": token,
                    "work": row["work_id"],
                    "owner": worker,
                    "revision": row["expected_revision"],
                    "mode": mode,
                },
            )
            return Claim(
                row["task_id"],
                row["work_id"],
                token,
                worker,
                row["expected_revision"],
                row["contract_version"],
                mode,
                row["kind"],
                row["input"],
                row["run_id"],
                row["trace_id"],
            )

    def _locked_claim(self, conn, claim):
        task = (
            conn.execute(
                text("""
            SELECT t.revision,t.current_version,t.owner_id,
                   c.state AS contract_state,c_claim.input AS claim_contract_input
            FROM tasks t JOIN task_contracts c
            ON c.task_id=t.task_id AND c.version=t.current_version
            JOIN task_contracts c_claim
            ON c_claim.task_id=t.task_id AND c_claim.version=:version
            WHERE t.task_id=:task FOR UPDATE OF t
            """),
                {"task": claim.task_id, "version": claim.contract_version},
            )
            .mappings()
            .first()
        )
        row = (
            conn.execute(
                text("""
            SELECT w.*, l.owner AS issued_owner,l.revision AS issued_revision,l.mode AS issued_mode,
                   (w.lease_until>clock_timestamp()) AS lease_live
            FROM work_items w JOIN work_leases l ON l.work_id=w.work_id
            WHERE w.work_id=:work AND l.token=:token FOR UPDATE OF w
            """),
                {"work": claim.work_id, "token": claim.token},
            )
            .mappings()
            .first()
        )
        if (
            task is None
            or row is None
            or row["task_id"] != claim.task_id
            or row["kind"] != claim.kind
            or row["issued_owner"] != claim.owner
            or row["issued_revision"] != claim.revision
            or row["contract_version"] != claim.contract_version
            or row["issued_mode"] != claim.mode
        ):
            raise Conflict("Unknown or mismatched lease")
        current = (
            row["lease_token"] == claim.token
            and row["state"] == "leased"
            and row["lease_live"]
            and task["revision"] == claim.revision
            and task["current_version"] == claim.contract_version
            and task["contract_state"] == KIND_CONTRACT_STATES.get(claim.kind)
        )
        # Claim.owner is worker identity; task ownership only comes from tasks.owner_id.
        return (
            {
                **row,
                "task_owner_id": task["owner_id"],
                "claim_contract_input": task["claim_contract_input"],
                "task_contract_state": task["contract_state"],
                "task_revision": task["revision"],
            },
            bool(current),
        )

    def renew(self, claim):
        with self.engine.begin() as conn:
            _, current = self._locked_claim(conn, claim)
            if not current:
                return False
            conn.execute(
                text("""
                UPDATE work_items SET lease_until=clock_timestamp()+(:seconds * interval '1 second')
                WHERE work_id=:work
                """),
                {"seconds": self.lease_seconds, "work": claim.work_id},
            )
            return True

    def record(self, claim, payload, outcome="ok"):
        if outcome not in {"ok", "error"} or payload.get("schema_version") != 1:
            raise ValueError("Versioned payload and known outcome required")
        if claim.kind != "form_goal":
            # read_source has no finalizer/reconciler yet: a free payload must not
            # close it or advance the task revision.
            raise Conflict("Generic receipts are limited to form_goal work")
        if claim.mode != "execute":
            raise Conflict("Reconciliation needs its own resolver; generic execution is forbidden")
        with self.engine.begin() as conn:
            _, current = self._locked_claim(conn, claim)
            return self._record_locked(conn, claim, current, payload, outcome)

    def _record_locked(
        self, conn, claim, current, payload, outcome, transition_kind="work_recorded"
    ):
        """Lease-level receipt write. Payloads never change formal goal state."""
        fingerprint = digest({"payload": payload, "outcome": outcome})
        prior = (
            conn.execute(
                text("""
            SELECT disposition,payload_hash FROM work_receipts WHERE lease_token=:token
            """),
                {"token": claim.token},
            )
            .mappings()
            .first()
        )
        if prior is not None:
            if prior["payload_hash"] != fingerprint:
                raise Conflict("Immutable receipt cannot be changed")
            return prior["disposition"]
        receipt_id = uuid4()
        disposition = "recorded" if current else "stale"
        conn.execute(
            text("""
            INSERT INTO work_receipts
                (receipt_id,lease_token,work_id,disposition,outcome,payload,payload_hash)
            VALUES (:id,:token,:work,:disposition,:outcome,CAST(:payload AS jsonb),:hash)
            """),
            {
                "id": receipt_id,
                "token": claim.token,
                "work": claim.work_id,
                "disposition": disposition,
                "outcome": outcome,
                "payload": encode(payload),
                "hash": fingerprint,
            },
        )
        if current:
            conn.execute(
                text("""
                UPDATE work_items SET state='closed',receipt_id=:receipt WHERE work_id=:work
                """),
                {"receipt": receipt_id, "work": claim.work_id},
            )
            conn.execute(
                text("UPDATE tasks SET revision=revision+1 WHERE task_id=:task"),
                {"task": claim.task_id},
            )
            self._transition(conn, claim.task_id, claim.revision + 1, transition_kind)
        return disposition

    def _end_goals(self, conn, task_id, version, state):
        conn.execute(
            text("""
            UPDATE goals SET state=:state
            WHERE task_id=:task AND contract_version=:version AND state='active'
            """),
            {"state": state, "task": task_id, "version": version},
        )

    def _end_blockers(self, conn, task_id, version, state):
        conn.execute(
            text("""
            UPDATE goal_blockers SET state=:state
            WHERE task_id=:task AND contract_version=:version AND state='open'
            """),
            {"state": state, "task": task_id, "version": version},
        )

    def form_decision(self, claim, allowed_owner, command):
        """Commit one immutable formation decision for the claim's contract version.

        ``command`` is a structured internal input (``models.FormationCommand``) whose
        review conclusions are supplied by controlled callers. A ``passed`` review flag
        is not proof of faithful semantics: the stored decision keeps that limit.
        """
        if not allowed_owner:
            raise ValueError("Controlled task owner scope required")
        if claim.mode != "execute":
            raise Conflict("Formation decisions need a live execute lease")
        payload = FormationCommand.model_validate(command).model_dump()
        if payload["candidate_fingerprint"] != digest(payload["candidate"]):
            raise Conflict("Formation candidate fingerprint mismatch")
        if not payload["input_contract_review"]["passed"] or not payload["policy_review"]["passed"]:
            raise Conflict("Formation command has not passed its reviews")
        decision, command_hash = payload["decision"], digest(payload)
        receipt = {
            "schema_version": 1,
            "stage": "goal_formation",
            "decision": decision,
            "command_hash": command_hash,
        }
        with self.engine.begin() as conn:
            work, current = self._locked_claim(conn, claim)
            if work["task_owner_id"] != allowed_owner:
                raise Conflict("Goal formation is not authorized for this task owner")
            if work["kind"] != "form_goal":
                raise Conflict("Goal formation only applies to form_goal work")
            expected_input = digest(claim.request)
            if payload["input_fingerprint"] != expected_input:
                raise Conflict("Formation input fingerprint mismatch")
            if digest(work["claim_contract_input"]) != expected_input:
                raise Conflict("Formation input fingerprint mismatch")
            for review in (payload["input_contract_review"], payload["policy_review"]):
                if (
                    review["input_fingerprint"] != payload["input_fingerprint"]
                    or review["candidate_fingerprint"] != payload["candidate_fingerprint"]
                ):
                    raise Conflict("Formation review fingerprint mismatch")
            stored = (
                conn.execute(
                    text("""
                SELECT formation_id,decision,command_hash FROM goal_formations
                WHERE task_id=:task AND contract_version=:version
                """),
                    {"task": claim.task_id, "version": claim.contract_version},
                )
                .mappings()
                .first()
            )
            if stored is not None:
                if stored["command_hash"] != command_hash:
                    raise Conflict("Formation decision already recorded with different content")
                prior_receipt = (
                    conn.execute(
                        text("""
                        SELECT disposition,payload FROM work_receipts
                        WHERE receipt_id=:receipt AND work_id=:work
                        """),
                        {"receipt": work["receipt_id"], "work": claim.work_id},
                    )
                    .mappings()
                    .first()
                )
                if (
                    work["state"] != "closed"
                    or prior_receipt is None
                    or prior_receipt["disposition"] != "recorded"
                    or prior_receipt["payload"].get("stage") != "goal_formation"
                    or prior_receipt["payload"].get("command_hash") != command_hash
                ):
                    raise Conflict("inconsistent_decision_state")
                return {
                    "decision": stored["decision"],
                    "formation_id": str(stored["formation_id"]),
                    "disposition": "recorded",
                    "idempotent": True,
                }
            if not current or work["task_contract_state"] != "forming":
                # Late or superseded output may leave an audit receipt, never a decision.
                disposition = self._record_locked(
                    conn, claim, False, {**receipt, "formation": "late_or_stale"}, "ok"
                )
                return {"decision": decision, "formation_id": None, "disposition": disposition}
            formation_id = uuid4()
            candidate = payload["candidate"]
            conn.execute(
                text("""
                INSERT INTO goal_formations
                (formation_id,task_id,contract_version,work_id,lease_token,revision,decision,
                 command,command_hash,input_fingerprint,candidate_fingerprint,
                 input_contract_review,policy_review)
                VALUES (:id,:task,:version,:work,:token,:revision,:decision,
                        CAST(:command AS jsonb),:hash,:input_fp,:candidate_fp,
                        CAST(:input_review AS jsonb),CAST(:policy_review AS jsonb))
                """),
                {
                    "id": formation_id,
                    "task": claim.task_id,
                    "version": claim.contract_version,
                    "work": claim.work_id,
                    "token": claim.token,
                    "revision": claim.revision,
                    "decision": decision,
                    "command": encode(payload),
                    "hash": command_hash,
                    "input_fp": payload["input_fingerprint"],
                    "candidate_fp": payload["candidate_fingerprint"],
                    "input_review": encode(payload["input_contract_review"]),
                    "policy_review": encode(payload["policy_review"]),
                },
            )
            if decision == "accept":
                for required, items in (
                    (True, candidate["required_results"]),
                    (False, candidate["optional_results"]),
                ):
                    for item in items:
                        conn.execute(
                            text("""
                            INSERT INTO goals
                            (goal_id,task_id,contract_version,required,title,acceptance,
                             basis,evidence,state,progress)
                            VALUES (:id,:task,:version,:required,:title,:acceptance,
                                    :basis,:evidence,'active','none')
                            """),
                            {
                                "id": uuid4(),
                                "task": claim.task_id,
                                "version": claim.contract_version,
                                "required": required,
                                "title": item["title"],
                                "acceptance": item["acceptance"],
                                "basis": item["basis"],
                                "evidence": item["evidence"],
                            },
                        )
                self._set_contract_state(conn, claim, "accepted", formation_id)
            elif decision == "needs_user":
                blocker = payload["blocker"]
                conn.execute(
                    text("""
                    INSERT INTO goal_blockers
                    (blocker_id,task_id,contract_version,question,waiting_condition,
                     responsible_party,advanceable,exit_path,state)
                    VALUES (:id,:task,:version,:question,:waiting,:party,:advanceable,
                            :exit_path,'open')
                    """),
                    {
                        "id": uuid4(),
                        "task": claim.task_id,
                        "version": claim.contract_version,
                        "question": blocker["question"],
                        "waiting": blocker["waiting_condition"],
                        "party": blocker["responsible_party"],
                        "advanceable": blocker["advanceable"],
                        "exit_path": blocker["exit_path"],
                    },
                )
            else:
                self._set_contract_state(conn, claim, "unformable", formation_id)
            disposition = self._record_locked(conn, claim, True, receipt, "ok", "goal_formation")
            if decision == "accept" and work["claim_contract_input"].get("output_kind") == "method_list":
                conn.execute(
                    text("""
                    INSERT INTO work_items
                    (work_id,task_id,contract_version,expected_revision,run_id,kind,state)
                    VALUES (:work,:task,:version,:revision,:run,'read_source','ready')
                    """),
                    {
                        "work": uuid4(),
                        "task": claim.task_id,
                        "version": claim.contract_version,
                        "revision": claim.revision + 1,
                        "run": claim.run_id,
                    },
                )
            return {
                "decision": decision,
                "formation_id": str(formation_id),
                "disposition": disposition,
            }

    def _set_contract_state(self, conn, claim, state, formation_id):
        changed = conn.execute(
            text("""
            UPDATE task_contracts SET state=:state
            WHERE task_id=:task AND version=:version AND state='forming'
            """),
            {"state": state, "task": claim.task_id, "version": claim.contract_version},
        ).rowcount
        if changed != 1:
            raise Conflict("Contract is no longer forming")

    def recover_form_goal(self, claim, allowed_owner):
        """Reconcile-only resolver for an expired ``form_goal`` lease.

        It decides from recorded facts only: it never runs a candidate, never calls
        ``record``, and resets the original work item only when every unretired
        source operation is provably undispatched (or none exists). Proven orphans
        are retired as failed pre-dispatch history in the same transaction, so a new
        execute lease may register a fresh operation. Every call leaves an audit row.
        """
        if not allowed_owner:
            raise ValueError("Controlled task owner scope required")
        if claim.mode != "reconcile":
            raise Conflict("Only a reconcile lease may resolve an expired form_goal")
        with self.engine.begin() as conn:
            work, current = self._locked_claim(conn, claim)
            if work["task_owner_id"] != allowed_owner:
                raise Conflict("Goal recovery is not authorized for this task owner")
            if work["kind"] != "form_goal":
                raise Conflict("Goal recovery only applies to form_goal work")
            decided = (
                conn.execute(
                    text(
                        "SELECT 1 FROM goal_formations WHERE task_id=:task"
                        " AND contract_version=:version"
                    ),
                    {"task": claim.task_id, "version": claim.contract_version},
                ).first()
                is not None
            )
            recorded_receipt = (
                conn.execute(
                    text("""
                SELECT 1 FROM work_receipts WHERE work_id=:work AND disposition='recorded'
                """),
                    {"work": claim.work_id},
                ).first()
                is not None
            )
            operations = (
                conn.execute(
                    text("""
                SELECT operation_id,phase,dispatched_at,outcome FROM source_reads
                WHERE work_id=:work AND NOT retired_pre_dispatch
                ORDER BY created_at,operation_id
                """),
                    {"work": claim.work_id},
                )
                .mappings()
                .all()
            )
            # Only rows whose facts prove the browser call never started are safe
            # to retire; any other unretired row blocks every retry (all or nothing).
            undispatched = [
                row["operation_id"]
                for row in operations
                if row["phase"] == "prepared"
                and row["dispatched_at"] is None
                and row["outcome"] == "none"
            ]
            blocking = len(undispatched) != len(operations)
            released = work["state"] == "ready" and work["lease_token"] is None
            if decided and work["state"] == "closed":
                action = "decision_already_recorded"
            elif decided:
                # Decision committed but work open: transaction consistency is broken.
                action = "inconsistent_decision_state"
            elif recorded_receipt:
                action = "receipt_already_recorded"
            elif blocking:
                action = "requires_source_reconciliation"
            elif released:
                action = "already_released"
            elif not current:
                action = "version_no_longer_current"
            else:
                action = "reset_to_ready"
            if action == "reset_to_ready":
                for operation_id in undispatched:
                    retired = conn.execute(
                        text("""
                        UPDATE source_reads
                        SET phase='closed',outcome='failed',disposition='stale',
                            error_code='interrupted_before_dispatch',retired_pre_dispatch=true
                        WHERE operation_id=:op AND NOT retired_pre_dispatch
                          AND phase='prepared' AND dispatched_at IS NULL AND outcome='none'
                        """),
                        {"op": operation_id},
                    ).rowcount
                    if retired != 1:
                        raise Conflict("Source operation changed during recovery")
                conn.execute(
                    text("""
                    UPDATE work_items SET state='ready',lease_token=NULL,lease_owner=NULL,
                        lease_until=NULL
                    WHERE work_id=:work AND state='leased'
                    """),
                    {"work": claim.work_id},
                )
            conn.execute(
                text("""
                INSERT INTO form_goal_recoveries
                (recovery_id,task_id,contract_version,revision,work_id,lease_token,action,detail)
                VALUES (:id,:task,:version,:revision,:work,:token,:action,CAST(:detail AS jsonb))
                """),
                {
                    "id": uuid4(),
                    "task": claim.task_id,
                    "version": claim.contract_version,
                    "revision": work["task_revision"],
                    "work": claim.work_id,
                    "token": claim.token,
                    "action": action,
                    "detail": encode(
                        {
                            "work_state": work["state"],
                            "lease_live": bool(work["lease_live"]),
                            "decision_recorded": decided,
                            "recorded_receipt": recorded_receipt,
                            "unretired_operations": len(operations),
                            "blocking_operations": blocking,
                            "retired_operations": [str(op) for op in undispatched]
                            if action == "reset_to_ready"
                            else [],
                            "current": bool(current),
                        }
                    ),
                },
            )
            return {
                "action": action,
                "work_id": str(claim.work_id),
                "reset": action == "reset_to_ready",
            }

    def source_action_candidate(self, claim, allowed_owner, action):
        """Derive the precise read scope of a live ``read_source`` lease.

        Pure data for the next responsibility area to re-validate: it writes no
        state, dispatches nothing and carries no credentials. ``allowed_owner`` is
        the trusted server-side task owner scope; ``Claim.owner`` is worker
        identity only and is never used as task ownership. Entry
        ``local_browser_actions`` is a scope ceiling, not a PermissionDecision.
        """
        if not allowed_owner:
            raise ValueError("Controlled task owner scope required")
        if action not in {"video", "subtitle", "audio_transcription"}:
            raise ValueError("Registered source actions only")
        if claim.kind != "read_source":
            raise Conflict("Source action candidates need read_source work")
        if claim.mode != "execute":
            raise Conflict("Source action candidates need an execute lease")
        with self.engine.begin() as conn:
            work, current = self._locked_claim(conn, claim)
            if work["task_owner_id"] != allowed_owner:
                raise Conflict("Source action candidate is not authorized for this task owner")
            if not current:
                raise Conflict("Source action candidate needs a current lease")
            contract_input = work["claim_contract_input"]
            if digest(claim.request) != digest(contract_input):
                raise Conflict("Source action candidate input fingerprint mismatch")
            scope = source_scope(contract_input, action)
            if action not in scope:
                raise Conflict("Action is outside the declared local browser scope")
            try:
                ref = VideoRef.parse(contract_input.get("source"))
            except SourceAccessError as exc:
                raise Conflict("Entry source is not a supported video") from exc
            return SourceActionCandidate(
                schema_version=1,
                task_id=claim.task_id,
                contract_version=claim.contract_version,
                revision=claim.revision,
                work_id=claim.work_id,
                action=action,
                bvid=ref.bvid,
                page=ref.page,
                input_fingerprint=digest(contract_input),
                entry_scope=tuple(scope),
            )

    def prepare_source_read(self, claim, allowed_owner, action):
        """Admit exactly one read-only source action without dispatching anything.

        One transaction commits three ordered transition commits (proposed,
        authorized, consumed) and creates the prepared Operation under its
        preallocated id. The returned Claim owns the work from now on; the claim
        passed in stops being current at commit time. The permission decision
        records the browser connection state as unverified, so nothing here is a
        dispatch right.
        """
        if not allowed_owner:
            raise ValueError("Controlled task owner scope required")
        if action not in {"video", "subtitle", "audio_transcription"}:
            raise ValueError("Registered source actions only")
        if claim.kind != "read_source":
            raise Conflict("Formal source admission needs read_source work")
        if claim.mode != "execute":
            raise Conflict("Formal source admission needs an execute lease")
        with self.engine.begin() as conn:
            work, current = self._locked_claim(conn, claim)
            if work["task_owner_id"] != allowed_owner:
                raise Conflict("Formal source admission is not authorized for this task owner")
            if not current:
                raise Conflict("Formal source admission needs a current lease")
            contract_input = work["claim_contract_input"]
            if digest(claim.request) != digest(contract_input):
                raise Conflict("Formal source admission input fingerprint mismatch")
            scope = source_scope(contract_input, action)
            if action not in scope:
                raise Conflict("Action is outside the declared local browser scope")
            try:
                ref = VideoRef.parse(contract_input.get("source"))
            except SourceAccessError as exc:
                raise Conflict("Entry source is not a supported video") from exc
            goals = [
                str(goal)
                for goal in conn.execute(
                    text("""
                    SELECT goal_id FROM goals
                    WHERE task_id=:task AND contract_version=:version AND required
                    ORDER BY goal_id
                    """),
                    {"task": claim.task_id, "version": claim.contract_version},
                ).scalars()
            ]
            if not goals:
                raise Conflict("Formal source admission needs the accepted required Goal set")
            input_fingerprint = digest(contract_input)
            existing = (
                conn.execute(
                    text("""
                SELECT p.proposal_id,p.operation_id,p.action,p.bvid,p.page,p.input_fingerprint,
                       d.permission_decision_id,d.owner_id,d.decision
                FROM source_proposals p
                JOIN source_permission_decisions d ON d.proposal_id=p.proposal_id
                WHERE p.work_id=:work
                """),
                    {"work": claim.work_id},
                )
                .mappings()
                .first()
            )
            if existing is not None:
                if (
                    existing["action"] != action
                    or existing["bvid"] != ref.bvid
                    or existing["page"] != ref.page
                    or existing["input_fingerprint"] != input_fingerprint
                    or existing["owner_id"] != allowed_owner
                    or existing["decision"] != "auto_allow"
                ):
                    raise Conflict("Formal source admission already recorded with different content")
                return SourceAdmission(
                    schema_version=1,
                    proposal_id=existing["proposal_id"],
                    permission_decision_id=existing["permission_decision_id"],
                    operation_id=existing["operation_id"],
                    action=action,
                    bvid=ref.bvid,
                    page=ref.page,
                    revision=claim.revision,
                    uniqueness_decision="reuse",
                    claim=claim,
                )
            proposal_id, decision_id, operation_id, attempt, token = (
                uuid4(),
                uuid4(),
                uuid4(),
                uuid4(),
                uuid4(),
            )
            goal_ref = encode({"required_goal_ids": goals})
            revision = claim.revision
            conn.execute(
                text("""
                INSERT INTO source_proposals
                (proposal_id,task_id,contract_version,revision,work_id,lease_token,operation_id,
                 state,action,bvid,page,effect,capability_version,policy_version,purpose,
                 stop_condition,time_limit,goal_ref,input_fingerprint)
                VALUES (:proposal,:task,:version,:revision,:work,:token,:operation,
                        'proposed',:action,:bvid,:page,'read_only',:capability,:policy,:purpose,
                        :stop,:time_limit,CAST(:goal_ref AS jsonb),:input_fp)
                """),
                {
                    "proposal": proposal_id,
                    "task": claim.task_id,
                    "version": claim.contract_version,
                    "revision": revision + 1,
                    "work": claim.work_id,
                    "token": claim.token,
                    "operation": operation_id,
                    "action": action,
                    "bvid": ref.bvid,
                    "page": ref.page,
                    "capability": source_adapter_version(action),
                    "policy": source_policy(action),
                    "purpose": SOURCE_READ_PURPOSE if action != "audio_transcription"
                    else "read the exact public audio and transcribe locally; no remote audio upload",
                    "stop": SOURCE_READ_STOP,
                    "time_limit": SOURCE_READ_TIME_LIMIT,
                    "goal_ref": goal_ref,
                    "input_fp": input_fingerprint,
                },
            )
            self._transition(
                conn,
                claim.task_id,
                revision + 1,
                "source_proposed",
                object_kind="source_proposal",
                object_ref=proposal_id,
                from_state="none",
                to_state="proposed",
                reason_ref=proposal_id,
                policy_version=source_policy(action),
                dedup_key=f"source_proposed:{proposal_id}",
            )
            conn.execute(
                text("""
                INSERT INTO source_permission_decisions
                (permission_decision_id,proposal_id,operation_id,task_id,contract_version,
                 revision,owner_id,authorization_basis,entry_scope,action,bvid,page,effect,
                 data_purpose,policy_version,decision,connection_state,reason_codes)
                VALUES (:decision,:proposal,:operation,:task,:version,:revision,:owner,
                        :basis,CAST(:scope AS jsonb),:action,:bvid,
                        :page,'read_only',:purpose,:policy,'auto_allow','unverified',
                        CAST(:reasons AS jsonb))
                """),
                {
                    "decision": decision_id,
                    "proposal": proposal_id,
                    "operation": operation_id,
                    "task": claim.task_id,
                    "version": claim.contract_version,
                    "revision": revision + 2,
                    "owner": allowed_owner,
                    "scope": encode(list(scope)),
                    "basis": "contract_local_source_actions" if action == "audio_transcription"
                    else "contract_local_browser_actions",
                    "action": action,
                    "bvid": ref.bvid,
                    "page": ref.page,
                    "purpose": SOURCE_READ_PURPOSE if action != "audio_transcription"
                    else "read the exact public audio and transcribe locally; no remote audio upload",
                    "policy": source_policy(action),
                    "reasons": encode(["entry_scope_allows_exact_read_only_action"]),
                },
            )
            conn.execute(
                text("UPDATE source_proposals SET state='authorized' WHERE proposal_id=:proposal"),
                {"proposal": proposal_id},
            )
            self._transition(
                conn,
                claim.task_id,
                revision + 2,
                "source_authorized",
                object_kind="source_proposal",
                object_ref=proposal_id,
                from_state="proposed",
                to_state="authorized",
                reason_ref=decision_id,
                policy_version=source_policy(action),
                dedup_key=f"source_authorized:{decision_id}",
            )
            conn.execute(
                text("""
                INSERT INTO work_leases (token,work_id,owner,revision,mode)
                VALUES (:token,:work,:owner,:revision,'execute')
                """),
                {
                    "token": token,
                    "work": claim.work_id,
                    "owner": claim.owner,
                    "revision": revision + 3,
                },
            )
            conn.execute(
                text("""
                INSERT INTO source_reads
                (operation_id,proposal_id,permission_id,attempt_id,admission_id,task_id,
                 contract_version,revision,work_id,lease_token,action,adapter_version,
                 proposal,permission,phase,outcome,permission_decision_id)
                VALUES (:operation,:proposal_id,:permission_id,:attempt,:admission,:task,
                        :version,:revision,:work,:token,:action,:adapter,CAST(:proposal AS jsonb),
                        CAST(:permission AS jsonb),'prepared','none',:decision)
                """),
                {
                    "operation": operation_id,
                    "proposal_id": proposal_id,
                    "permission_id": decision_id,
                    "attempt": attempt,
                    "admission": uuid4(),
                    "task": claim.task_id,
                    "version": claim.contract_version,
                    "revision": revision + 3,
                    "work": claim.work_id,
                    "token": token,
                    "action": action,
                    "adapter": source_adapter_version(action),
                    "proposal": encode(
                        {
                            "schema_version": 1,
                            "purpose": "read_declared_source_for_accepted_goals",
                            "source": {"bvid": ref.bvid, "page": ref.page},
                            "action": action,
                            "capability_version": source_adapter_version(action),
                        }
                    ),
                    "permission": encode(
                        {
                            "schema_version": 1,
                            "disposition": "auto_allow",
                            "effect": "read_only",
                            "policy": source_policy(action),
                            "permission_decision_id": str(decision_id),
                            "connection_state": "unverified",
                        }
                    ),
                    "decision": decision_id,
                },
            )
            conn.execute(
                text("""
                INSERT INTO source_operation_attempts
                (attempt_id,operation_id,attempt_seq,work_id,lease_token,state,
                 dispatch_admission_obtained,close_reason,created_at,closed_at)
                VALUES (:attempt,:operation,1,:work,:token,'prepared',false,NULL,
                        clock_timestamp(),NULL)
                """),
                {
                    "attempt": attempt,
                    "operation": operation_id,
                    "work": claim.work_id,
                    "token": token,
                },
            )
            conn.execute(
                text("UPDATE source_proposals SET state='consumed' WHERE proposal_id=:proposal"),
                {"proposal": proposal_id},
            )
            self._transition(
                conn,
                claim.task_id,
                revision + 3,
                "source_consumed",
                object_kind="source_proposal",
                object_ref=proposal_id,
                from_state="authorized",
                to_state="consumed",
                reason_ref=decision_id,
                policy_version=source_policy(action),
                dedup_key=f"source_consumed:{operation_id}",
            )
            moved = conn.execute(
                text("UPDATE tasks SET revision=revision+3 WHERE task_id=:task AND revision=:revision"),
                {"task": claim.task_id, "revision": revision},
            ).rowcount
            if moved != 1:
                raise Conflict("Task revision moved during source admission")
            leased = conn.execute(
                text("""
                UPDATE work_items SET lease_token=:token,lease_owner=:owner,
                    lease_until=clock_timestamp()+(:seconds * interval '1 second'),
                    expected_revision=:expected
                WHERE work_id=:work AND state='leased' AND lease_token=:old
                """),
                {
                    "token": token,
                    "owner": claim.owner,
                    "seconds": self.lease_seconds,
                    "expected": revision + 3,
                    "work": claim.work_id,
                    "old": claim.token,
                },
            ).rowcount
            if leased != 1:
                raise Conflict("Source admission lease changed during commit")
            return SourceAdmission(
                schema_version=1,
                proposal_id=proposal_id,
                permission_decision_id=decision_id,
                operation_id=operation_id,
                action=action,
                bvid=ref.bvid,
                page=ref.page,
                revision=revision + 3,
                uniqueness_decision="new",
                claim=Claim(
                    claim.task_id,
                    claim.work_id,
                    token,
                    claim.owner,
                    revision + 3,
                    claim.contract_version,
                    "execute",
                    claim.kind,
                    claim.request,
                    claim.run_id,
                    claim.trace_id,
                ),
            )

    def _attempt_view(self, conn, attempt_id):
        return (
            conn.execute(
                text("""
            SELECT a.attempt_id,a.operation_id,a.attempt_seq,a.work_id,a.lease_token,
                   a.state,a.dispatch_admission_obtained,a.close_reason,
                   l.owner AS lease_owner,l.revision AS lease_revision,l.mode AS lease_mode,
                    w.run_id,w.task_id AS work_task_id,w.contract_version AS work_version,
                    w.state AS work_state,w.lease_token AS work_lease_token,
                    (w.lease_until>clock_timestamp()) AS work_lease_live,
                    w.expected_revision,t.trace_id,t.revision AS task_revision,
                    t.current_version,c.state AS contract_state,c.input AS contract_input,
                    r.attempt_id AS projected_attempt_id,r.lease_token AS projected_lease_token,
                    r.phase AS operation_phase,r.outcome AS operation_outcome,
                    r.dispatched_at,r.received_at,r.raw_payload,r.material,r.payload_hash,
                    r.task_id AS operation_task_id,r.contract_version AS operation_version,
                    r.work_id AS operation_work_id,r.action AS operation_action,
                    p.state AS proposal_state,p.work_id AS proposal_work_id,
                    p.input_fingerprint,p.action AS proposal_action,
                    p.policy_version AS proposal_policy,
                    d.decision AS permission_decision,d.owner_id AS permission_owner,
                    d.action AS permission_action,d.policy_version AS permission_policy,
                    d.entry_scope AS permission_entry_scope
            FROM source_operation_attempts a
            JOIN work_leases l ON l.token=a.lease_token
            JOIN work_items w ON w.work_id=a.work_id
            JOIN tasks t ON t.task_id=w.task_id
            JOIN task_contracts c ON c.task_id=t.task_id AND c.version=t.current_version
            JOIN source_reads r ON r.operation_id=a.operation_id
            LEFT JOIN source_proposals p
              ON p.proposal_id=r.proposal_id AND p.operation_id=r.operation_id
            LEFT JOIN source_permission_decisions d
              ON d.permission_decision_id=r.permission_decision_id
             AND d.proposal_id=p.proposal_id AND d.operation_id=r.operation_id
            WHERE a.attempt_id=:attempt
            FOR UPDATE OF a,r
            """),
                {"attempt": attempt_id},
            )
            .mappings()
            .one()
        )

    def _claim_from_attempt(self, claim, view):
        return Claim(
            claim.task_id,
            claim.work_id,
            view["lease_token"],
            view["lease_owner"],
            view["lease_revision"],
            claim.contract_version,
            view["lease_mode"],
            claim.kind,
            view["contract_input"],
            view["run_id"],
            view["trace_id"],
        )

    def recover_source_read(self, claim, allowed_owner):
        """Same-identity recovery of a formal prepared source operation.

        Only when persistent facts prove nothing was ever dispatched: the old
        attempt closes as interrupted before dispatch, the next attempt of the
        SAME operation opens, and a fresh execute lease is issued. Anything
        possibly dispatched or factually unclear stays unretried, and no second
        Proposal/PermissionDecision/Operation appears. This grants no dispatch
        right.
        """
        if not allowed_owner:
            raise ValueError("Controlled task owner scope required")
        if claim.kind != "read_source":
            raise Conflict("Source recovery only applies to read_source work")
        if claim.mode != "reconcile":
            raise Conflict("Only a reconcile lease may recover a source operation")
        with self.engine.begin() as conn:
            work, current = self._locked_claim(conn, claim)
            if work["task_owner_id"] != allowed_owner:
                raise Conflict("Source recovery is not authorized for this task owner")
            done = conn.execute(
                text(
                    "SELECT object_ref FROM transitions"
                    " WHERE task_id=:task AND dedup_key=:key"
                ),
                {"task": claim.task_id, "key": f"source_attempt_recovered:{claim.token}"},
            ).scalar_one_or_none()
            if done is not None:
                # A crash-resend of the same reconcile lease never advances twice.
                view = self._attempt_view(conn, UUID(str(done)))
                if not (
                    view["work_id"] == claim.work_id
                    and view["work_task_id"] == claim.task_id
                    and view["work_version"] == claim.contract_version
                    and view["current_version"] == claim.contract_version
                    and view["contract_state"] == "accepted"
                    and view["work_state"] == "leased"
                    and view["work_lease_token"] == view["lease_token"]
                    and view["work_lease_live"]
                    and view["expected_revision"] == view["task_revision"]
                    == view["lease_revision"]
                    and view["lease_mode"] == "execute"
                    and view["state"] == "prepared"
                    and view["dispatch_admission_obtained"] is False
                    and view["projected_attempt_id"] == view["attempt_id"]
                    and view["projected_lease_token"] == view["lease_token"]
                    and view["operation_phase"] == "prepared"
                    and view["operation_outcome"] == "none"
                    and view["dispatched_at"] is None
                    and view["received_at"] is None
                    and view["raw_payload"] is None
                    and view["material"] is None
                    and view["payload_hash"] is None
                    and view["operation_task_id"] == claim.task_id
                    and view["operation_version"] == claim.contract_version
                    and view["operation_work_id"] == claim.work_id
                    and view["proposal_state"] == "consumed"
                    and view["proposal_work_id"] == claim.work_id
                    and view["input_fingerprint"] == digest(view["contract_input"])
                    and view["proposal_action"] == view["operation_action"]
                    and view["proposal_policy"] == source_policy(view["operation_action"])
                    and view["permission_decision"] == "auto_allow"
                    and view["permission_owner"] == allowed_owner
                    and view["permission_action"] == view["operation_action"]
                    and view["permission_policy"] == source_policy(view["operation_action"])
                    and view["operation_action"]
                    in source_scope(view["contract_input"], view["operation_action"])
                    and view["operation_action"] in (view["permission_entry_scope"] or [])
                    and digest(claim.request) == digest(view["contract_input"])
                ):
                    return SourceRecovery(
                        schema_version=1,
                        action="version_no_longer_current",
                        work_id=claim.work_id,
                        operation_id=view["operation_id"],
                        attempt_id=view["attempt_id"],
                        attempt_seq=view["attempt_seq"],
                        revision=view["task_revision"],
                        idempotent=False,
                        claim=None,
                    )
                return SourceRecovery(
                    schema_version=1,
                    action="attempt_recovered",
                    work_id=claim.work_id,
                    operation_id=view["operation_id"],
                    attempt_id=view["attempt_id"],
                    attempt_seq=view["attempt_seq"],
                    revision=view["lease_revision"],
                    idempotent=True,
                    claim=self._claim_from_attempt(claim, view),
                )
            operations = (
                conn.execute(
                    text("""
                SELECT r.operation_id,r.attempt_id,r.lease_token,r.phase,r.outcome,
                       r.dispatched_at,r.received_at,r.raw_payload,r.material,r.payload_hash,
                       r.permission_decision_id,r.proposal_id,r.permission_id,r.task_id,
                       r.contract_version,r.work_id,r.action,
                       p.state AS proposal_state,p.task_id AS proposal_task_id,
                       p.contract_version AS proposal_version,p.work_id AS proposal_work_id,
                       p.input_fingerprint,p.action AS proposal_action,
                       p.policy_version AS proposal_policy,
                       d.decision AS permission_decision,d.owner_id AS permission_owner,
                       d.task_id AS permission_task_id,
                       d.contract_version AS permission_version,
                       d.action AS permission_action,d.policy_version AS permission_policy,
                       d.entry_scope AS permission_entry_scope
                FROM source_reads r
                LEFT JOIN source_proposals p
                  ON p.proposal_id=r.proposal_id AND p.operation_id=r.operation_id
                LEFT JOIN source_permission_decisions d
                  ON d.permission_decision_id=r.permission_decision_id
                 AND d.proposal_id=p.proposal_id AND d.operation_id=r.operation_id
                WHERE r.work_id=:work ORDER BY r.created_at,r.operation_id
                FOR UPDATE OF r
                """),
                    {"work": claim.work_id},
                )
                .mappings()
                .all()
            )
            if not operations:
                if not current:
                    return SourceRecovery(
                        schema_version=1,
                        action="version_no_longer_current",
                        work_id=claim.work_id,
                        operation_id=None,
                        attempt_id=None,
                        attempt_seq=None,
                        revision=work["task_revision"],
                        idempotent=False,
                        claim=None,
                    )
                if work["state"] == "leased":
                    conn.execute(
                        text("""
                        UPDATE work_items SET state='ready',lease_token=NULL,lease_owner=NULL,
                            lease_until=NULL
                        WHERE work_id=:work AND state='leased'
                        """),
                        {"work": claim.work_id},
                    )
                return SourceRecovery(
                    schema_version=1,
                    action="reset_to_ready",
                    work_id=claim.work_id,
                    operation_id=None,
                    attempt_id=None,
                    attempt_seq=None,
                    revision=work["task_revision"],
                    idempotent=False,
                    claim=None,
                )
            row = operations[0]
            head = (
                conn.execute(
                    text("""
                    SELECT a.*,l.work_id AS attempt_lease_work_id,
                           l.mode AS attempt_lease_mode
                    FROM source_operation_attempts a
                    JOIN work_leases l ON l.token=a.lease_token
                    WHERE a.attempt_id=:attempt FOR UPDATE OF a
                    """),
                    {"attempt": row["attempt_id"]},
                )
                .mappings()
                .first()
            )
            provably_undispatched = (
                len(operations) == 1
                and row["permission_decision_id"] is not None
                and row["task_id"] == claim.task_id
                and row["contract_version"] == claim.contract_version
                and row["work_id"] == claim.work_id
                and row["proposal_state"] == "consumed"
                and row["proposal_task_id"] == claim.task_id
                and row["proposal_version"] == claim.contract_version
                and row["proposal_work_id"] == claim.work_id
                and row["input_fingerprint"] == digest(work["claim_contract_input"])
                and row["proposal_action"] == row["action"]
                and row["proposal_policy"] == source_policy(row["action"])
                and row["permission_decision"] == "auto_allow"
                and row["permission_owner"] == allowed_owner
                and row["permission_task_id"] == claim.task_id
                and row["permission_version"] == claim.contract_version
                and row["permission_action"] == row["action"]
                and row["permission_policy"] == source_policy(row["action"])
                and row["action"] in source_scope(work["claim_contract_input"], row["action"])
                and row["action"] in (row["permission_entry_scope"] or [])
                and row["phase"] == "prepared"
                and row["outcome"] == "none"
                and row["dispatched_at"] is None
                and row["received_at"] is None
                and row["raw_payload"] is None
                and row["material"] is None
                and row["payload_hash"] is None
                and head is not None
                and head["operation_id"] == row["operation_id"]
                and head["work_id"] == claim.work_id
                and head["lease_token"] == row["lease_token"]
                and head["attempt_lease_work_id"] == claim.work_id
                and head["attempt_lease_mode"] == "execute"
                and head["state"] == "prepared"
                and head["dispatch_admission_obtained"] is False
            )
            if not provably_undispatched:
                # Possibly dispatched, already closed, or facts contradict the head.
                return SourceRecovery(
                    schema_version=1,
                    action="requires_source_reconciliation",
                    work_id=claim.work_id,
                    operation_id=row["operation_id"],
                    attempt_id=row["attempt_id"],
                    attempt_seq=head["attempt_seq"] if head is not None else None,
                    revision=work["task_revision"],
                    idempotent=False,
                    claim=None,
                )
            if not current:
                return SourceRecovery(
                    schema_version=1,
                    action="version_no_longer_current",
                    work_id=claim.work_id,
                    operation_id=row["operation_id"],
                    attempt_id=row["attempt_id"],
                    attempt_seq=head["attempt_seq"],
                    revision=work["task_revision"],
                    idempotent=False,
                    claim=None,
                )
            revision = claim.revision
            new_attempt_id, token = uuid4(), uuid4()
            conn.execute(
                text("""
                INSERT INTO work_leases (token,work_id,owner,revision,mode)
                VALUES (:token,:work,:owner,:revision,'execute')
                """),
                {
                    "token": token,
                    "work": claim.work_id,
                    "owner": claim.owner,
                    "revision": revision + 1,
                },
            )
            closed = conn.execute(
                text("""
                UPDATE source_operation_attempts
                SET state='closed',close_reason='interrupted_before_dispatch',
                    closed_at=clock_timestamp()
                WHERE attempt_id=:attempt AND operation_id=:operation AND state='prepared'
                  AND dispatch_admission_obtained IS FALSE
                """),
                {"attempt": row["attempt_id"], "operation": row["operation_id"]},
            ).rowcount
            if closed != 1:
                raise Conflict("Source attempt changed during recovery")
            conn.execute(
                text("""
                INSERT INTO source_operation_attempts
                (attempt_id,operation_id,attempt_seq,work_id,lease_token,state,
                 dispatch_admission_obtained,close_reason,created_at,closed_at)
                VALUES (:attempt,:operation,:seq,:work,:token,'prepared',false,NULL,
                        clock_timestamp(),NULL)
                """),
                {
                    "attempt": new_attempt_id,
                    "operation": row["operation_id"],
                    "seq": head["attempt_seq"] + 1,
                    "work": claim.work_id,
                    "token": token,
                },
            )
            projected = conn.execute(
                text("""
                UPDATE source_reads SET attempt_id=:attempt,lease_token=:token
                WHERE operation_id=:operation AND attempt_id=:old_attempt
                  AND lease_token=:old_lease AND phase='prepared' AND outcome='none'
                  AND dispatched_at IS NULL AND received_at IS NULL
                  AND raw_payload IS NULL AND material IS NULL AND payload_hash IS NULL
                """),
                {
                    "attempt": new_attempt_id,
                    "token": token,
                    "operation": row["operation_id"],
                    "old_attempt": row["attempt_id"],
                    "old_lease": row["lease_token"],
                },
            ).rowcount
            if projected != 1:
                raise Conflict("Source operation changed during recovery")
            self._transition(
                conn,
                claim.task_id,
                revision + 1,
                "source_attempt_recovered",
                object_kind="source_operation_attempt",
                object_ref=new_attempt_id,
                from_state=f"attempt:{row['attempt_id']}",
                to_state=f"attempt:{new_attempt_id}",
                reason_ref=row["permission_decision_id"],
                policy_version=source_policy(row["action"]),
                dedup_key=f"source_attempt_recovered:{claim.token}",
            )
            moved = conn.execute(
                text("UPDATE tasks SET revision=revision+1 WHERE task_id=:task AND revision=:revision"),
                {"task": claim.task_id, "revision": revision},
            ).rowcount
            if moved != 1:
                raise Conflict("Task revision moved during source recovery")
            leased = conn.execute(
                text("""
                UPDATE work_items SET lease_token=:token,lease_owner=:owner,
                    lease_until=clock_timestamp()+(:seconds * interval '1 second'),
                    expected_revision=:expected
                WHERE work_id=:work AND state='leased' AND lease_token=:old
                """),
                {
                    "token": token,
                    "owner": claim.owner,
                    "seconds": self.lease_seconds,
                    "expected": revision + 1,
                    "work": claim.work_id,
                    "old": claim.token,
                },
            ).rowcount
            if leased != 1:
                raise Conflict("Source recovery lease changed during commit")
            return SourceRecovery(
                schema_version=1,
                action="attempt_recovered",
                work_id=claim.work_id,
                operation_id=row["operation_id"],
                attempt_id=new_attempt_id,
                attempt_seq=head["attempt_seq"] + 1,
                revision=revision + 1,
                idempotent=False,
                claim=Claim(
                    claim.task_id,
                    claim.work_id,
                    token,
                    claim.owner,
                    revision + 1,
                    claim.contract_version,
                    "execute",
                    claim.kind,
                    claim.request,
                    claim.run_id,
                    claim.trace_id,
                ),
            )

    # ------------------------------------------------------------------
    # Controlled formal source exit (GOAL-015)
    #
    # Production entry stays closed: no handler is registered for
    # ``build_artifact``, and nothing here calls OpenCLI or a browser. These
    # methods only move formal state inside one PostgreSQL transaction using the
    # task -> work lock order.
    # ------------------------------------------------------------------

    def _exit_scope(self, conn, claim, allowed_owner, kind, require_current=True):
        """Task -> work lock order plus live lease and owner/contract identity.

        ``require_current`` is relaxed only for the idempotent re-send of a
        settled result: the first settlement necessarily ends the lease, so the
        identical re-send must still be answerable. Anything that writes state
        keeps the live-lease requirement.
        """
        if not allowed_owner:
            raise ValueError("Controlled task owner scope required")
        if claim.mode != "execute":
            raise Conflict("Formal source exit needs a live execute lease")
        work, current = self._locked_claim(conn, claim)
        if work["task_owner_id"] != allowed_owner:
            raise Conflict("Formal source exit is not authorized for this task owner")
        if work["kind"] != kind:
            raise Conflict(f"Formal source exit needs {kind} work")
        if require_current and not current:
            raise Conflict("Formal source exit needs a current lease")
        return {**work, "current": bool(current)}

    def _operation_for_exit(self, conn, claim, operation_id):
        row = (
            conn.execute(
                text("SELECT * FROM source_reads WHERE operation_id=:op FOR UPDATE"),
                {"op": operation_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("Unknown source operation")
        if (
            row["lease_token"] != claim.token
            or row["task_id"] != claim.task_id
            or row["work_id"] != claim.work_id
        ):
            raise Conflict("Source operation identity mismatch")
        # The admitted scope lives in the proposal; surface it so callers can
        # compare a result against the exact BVID/page that was admitted.
        scope = dict(row)
        source = row["proposal"]["source"]
        scope["bvid"] = source["bvid"]
        scope["page"] = source["page"]
        return scope

    def _current_attempt(self, conn, claim, operation_id, attempt_id):
        row = (
            conn.execute(
                text("""
            SELECT * FROM source_operation_attempts
            WHERE operation_id=:op AND attempt_id=:att FOR UPDATE
            """),
                {"op": operation_id, "att": attempt_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["lease_token"] != claim.token or row["work_id"] != claim.work_id:
            raise Conflict("Attempt identity mismatch")
        if row["state"] != "prepared":
            raise Conflict("Attempt is no longer prepared")
        return row

    def admit_dispatch(self, claim, allowed_owner, operation_id, attempt_id, session_proof):
        """Grant one-shot permission to send exactly one source request.

        The ``dispatching`` phase and the admission row are persisted before any
        external I/O happens. A caller that cannot supply an observable session
        proof gets an honest ``refused`` admission and a non-sendable ticket: no
        credential is invented, and ``connection_state='unverified'`` or an
        ``auto_allow`` record is never accepted as online proof.
        """
        proof = None
        proof_kind = None
        refusal = None
        if session_proof is not None:
            try:
                if isinstance(session_proof, dict) and session_proof.get("access_mode") == "anonymous_public_client":
                    proof = DispatchAudioProof.model_validate(session_proof).model_dump()
                    proof_kind = "observed_anonymous_audio_identity"
                else:
                    proof = DispatchSessionProof.model_validate(session_proof).model_dump()
                    proof_kind = "observed_browser_session"
            except ValidationError:
                refusal = "session_proof_unverifiable"
        else:
            refusal = "session_proof_absent"

        with self.engine.begin() as conn:
            work = self._exit_scope(conn, claim, allowed_owner, "read_source")
            operation = self._operation_for_exit(conn, claim, operation_id)
            if operation["action"] == "audio_transcription":
                if (proof_kind != "observed_anonymous_audio_identity" or proof is None
                        or (proof["bvid"], proof["page"]) != (operation["bvid"], operation["page"])):
                    proof, proof_kind, refusal = None, None, "audio_identity_unverifiable"
            elif proof_kind == "observed_anonymous_audio_identity":
                proof, proof_kind, refusal = None, None, "session_proof_unverifiable"
            attempt = self._current_attempt(conn, claim, operation_id, attempt_id)
            # At most one send right per attempt. Checked before the phase gate so
            # concurrent callers and resends re-read the same ticket instead of
            # treating the already-granted operation as unprepared.
            live = (
                conn.execute(
                    text("""
                SELECT * FROM source_dispatch_admissions
                WHERE attempt_id=:att AND state IN ('granted','consumed')
                """),
                    {"att": attempt_id},
                )
                .mappings()
                .first()
            )
            if live is not None:
                if operation["action"] == "audio_transcription" and proof != live["session_proof"]:
                    return self._ticket(live, allowed_owner, sendable=False)
                return self._ticket(
                    live, allowed_owner,
                    sendable=refusal is None and live["state"] == "granted",
                )
            if operation["phase"] != "prepared":
                raise Conflict("Dispatch admission needs a prepared operation")
            contract_input = work["claim_contract_input"]
            if digest(claim.request) != digest(contract_input):
                raise Conflict("Dispatch admission input fingerprint mismatch")
            scope = source_scope(contract_input, operation["action"])
            if operation["action"] not in scope:
                raise Conflict("Action is outside the declared local browser scope")
            goals = conn.execute(
                text("""
                SELECT count(*) FROM goals
                WHERE task_id=:task AND contract_version=:version AND required AND state='active'
                """),
                {"task": claim.task_id, "version": claim.contract_version},
            ).scalar_one()
            if not goals:
                raise Conflict("Dispatch admission needs the accepted required Goal set")
            if conn.execute(
                text("SELECT count(*) FROM goals WHERE task_id=:task AND contract_version=:version"
                     " AND required AND state IN ('canceled','superseded')"),
                {"task": claim.task_id, "version": claim.contract_version},
            ).scalar_one():
                raise Conflict("Required Goal set is no longer current")

            generation = operation["dispatch_generation"] + 1
            admission_id = uuid4()
            state = "granted" if refusal is None else "refused"
            conn.execute(
                text("""
                INSERT INTO source_dispatch_admissions
                (admission_id,operation_id,attempt_id,attempt_seq,task_id,contract_version,
                 revision,work_id,lease_token,action,bvid,page,dispatch_generation,state,
                 refusal_code,session_proof_kind,session_proof,checked_connection_state)
                VALUES (:id,:op,:att,:seq,:task,:version,:revision,:work,:token,:action,
                        :bvid,:page,:gen,:state,:refusal,:proof_kind,CAST(:proof AS jsonb),
                        :connection)
                """),
                {
                    "id": admission_id,
                    "op": operation_id,
                    "att": attempt_id,
                    "seq": attempt["attempt_seq"],
                    "task": claim.task_id,
                    "version": claim.contract_version,
                    "revision": claim.revision,
                    "work": claim.work_id,
                    "token": claim.token,
                    "action": operation["action"],
                    "bvid": operation["bvid"],
                    "page": operation["page"],
                    "gen": generation,
                    "state": state,
                    "refusal": refusal,
                    "proof_kind": proof_kind,
                    "proof": encode(proof) if proof is not None else None,
                    "connection": "verified" if proof is not None else "unverified",
                },
            )
            # The admission row IS the durable record of this decision. No
            # TransitionCommit here: ``transitions`` allows one commit per
            # revision and this revision is the one ``prepare_source_read``
            # already committed. Settlement writes its own commit at the bumped
            # revision.
            if refusal is not None:
                # Controlled, non-sendable stop: no dispatching phase is written
                # and nothing may be sent outside.
                return DispatchTicket(
                    schema_version=1,
                    admission_id=admission_id,
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    attempt_seq=attempt["attempt_seq"],
                    dispatch_generation=generation,
                    task_id=claim.task_id,
                    work_id=claim.work_id,
                    contract_version=claim.contract_version,
                    revision=claim.revision,
                    owner=allowed_owner,
                    action=operation["action"],
                    bvid=operation["bvid"],
                    page=operation["page"],
                    lease_token=claim.token,
                    session_proof_kind="",
                    sendable=False,
                )
            conn.execute(
                text("""
                UPDATE source_reads SET phase='dispatching',dispatched_at=clock_timestamp(),
                    dispatch_generation=:gen WHERE operation_id=:op
                """),
                {"gen": generation, "op": operation_id},
            )
            conn.execute(
                text("""
                UPDATE source_operation_attempts SET dispatch_admission_obtained=true
                WHERE attempt_id=:att
                """),
                {"att": attempt_id},
            )
            return DispatchTicket(
                schema_version=1,
                admission_id=admission_id,
                operation_id=operation_id,
                attempt_id=attempt_id,
                attempt_seq=attempt["attempt_seq"],
                dispatch_generation=generation,
                task_id=claim.task_id,
                work_id=claim.work_id,
                contract_version=claim.contract_version,
                revision=claim.revision,
                owner=allowed_owner,
                action=operation["action"],
                bvid=operation["bvid"],
                page=operation["page"],
                lease_token=claim.token,
                session_proof_kind=proof_kind,
                sendable=True,
            )

    def _ticket(self, row, owner, sendable):
        return DispatchTicket(
            schema_version=1,
            admission_id=row["admission_id"],
            operation_id=row["operation_id"],
            attempt_id=row["attempt_id"],
            attempt_seq=row["attempt_seq"],
            dispatch_generation=row["dispatch_generation"],
            task_id=row["task_id"],
            work_id=row["work_id"],
            contract_version=row["contract_version"],
            revision=row["revision"],
            owner=owner,
            action=row["action"],
            bvid=row["bvid"],
            page=row["page"],
            lease_token=row["lease_token"],
            session_proof_kind=row["session_proof_kind"] or "",
            sendable=sendable,
        )

    def consume_dispatch(self, claim, allowed_owner, ticket):
        """Use a granted dispatch ticket at most once, immediately before external I/O."""
        if not ticket.sendable:
            raise Conflict("A non-sendable ticket cannot be consumed")
        with self.engine.begin() as conn:
            work = self._exit_scope(conn, claim, allowed_owner, "read_source")
            row = conn.execute(
                text("""
                SELECT a.*,r.phase,r.outcome,r.permission_decision_id,
                       p.owner_id AS permission_owner,p.decision AS permission_decision
                FROM source_dispatch_admissions a
                JOIN source_reads r ON r.operation_id=a.operation_id
                JOIN source_permission_decisions p
                  ON p.permission_decision_id=r.permission_decision_id
                WHERE a.admission_id=:id FOR UPDATE OF a
                """),
                {"id": ticket.admission_id},
            ).mappings().one_or_none()
            if row is None:
                raise Conflict("Unknown dispatch ticket")
            expected = {
                "operation_id": ticket.operation_id, "attempt_id": ticket.attempt_id,
                "task_id": claim.task_id, "work_id": claim.work_id,
                "contract_version": claim.contract_version, "revision": claim.revision,
                "lease_token": claim.token, "action": ticket.action, "bvid": ticket.bvid,
                "page": ticket.page, "dispatch_generation": ticket.dispatch_generation,
            }
            if (
                any(row[key] != value for key, value in expected.items())
                or ticket.owner != allowed_owner or work["task_owner_id"] != allowed_owner
                or row["permission_owner"] != allowed_owner
                or row["permission_decision"] != "auto_allow"
                or row["checked_connection_state"] != "verified"
                or row["phase"] != "dispatching" or row["outcome"] != "none"
                or row["state"] not in {"granted", "consumed"}
            ):
                raise Conflict("Dispatch ticket no longer matches the authorized operation")
            if row["state"] == "consumed":
                return False
            updated = conn.execute(
                text("""
                UPDATE source_dispatch_admissions SET state='consumed',
                    consumed_at=clock_timestamp()
                WHERE admission_id=:id AND state='granted'
                """),
                {"id": ticket.admission_id},
            ).rowcount
            return updated == 1

    def _admission_for(self, conn, attempt_id):
        row = (
            conn.execute(
                text("""
            SELECT * FROM source_dispatch_admissions
            WHERE attempt_id=:att AND state IN ('granted','consumed')
            """),
                {"att": attempt_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise Conflict("No sendable dispatch admission for this operation")
        return row

    def settle_source_result(self, claim, allowed_owner, settlement):
        """Adjudicate one verified subtitle reply into formal material, once.

        Saving the material, closing ``read_source``, enqueueing the single
        ``build_artifact`` successor and the TransitionCommit happen in one
        transaction. Re-sending the identical reply is idempotent; a different
        body for the same operation conflicts.
        """
        data = SourceSettlementInput.model_validate(settlement).model_dump()
        if data["adapter_version"] != source_adapter_version(data["action"]):
            raise Conflict("Adapter version changed since this operation was admitted")
        operation_id = UUID(data["operation_id"])
        with self.engine.begin() as conn:
            work = self._exit_scope(
                conn, claim, allowed_owner, "read_source", require_current=False
            )
            operation = self._operation_for_exit(conn, claim, operation_id)
            if (
                operation["action"] != data["action"]
                or operation["adapter_version"] != data["adapter_version"]
                or operation["bvid"] != data["bvid"]
                or operation["page"] != data["page"]
            ):
                raise Conflict("Result does not match the admitted source scope")
            contract_input = work["claim_contract_input"]
            if digest(claim.request) != digest(contract_input):
                raise Conflict("Formal result input fingerprint mismatch")
            try:
                ref = VideoRef.parse(contract_input.get("source"))
            except SourceAccessError as exc:
                raise Conflict("Entry source is not a supported video") from exc
            if ref.bvid != data["bvid"] or ref.page != data["page"]:
                raise Conflict("Result does not match the entry source")
            raw_bytes = data["raw_reply"].encode("utf-8")
            fingerprint = payload_hash(raw_bytes)
            prior = (
                conn.execute(
                    text("SELECT * FROM source_formal_materials WHERE operation_id=:op"),
                    {"op": operation_id},
                )
                .mappings()
                .first()
            )
            if prior is not None:
                # The identical re-send is answered from the recorded material
                # even though its lease is long gone; a different body conflicts.
                if prior["payload_hash"] != fingerprint:
                    raise Conflict("Immutable formal material cannot be changed")
                return SourceSettlement(
                    schema_version=1,
                    disposition=prior["material_state"],
                    material_id=prior["material_id"],
                    payload_hash=prior["payload_hash"],
                    segments=len(prior["segments"]),
                    successor_work_id=prior["successor_work_id"],
                    expected_revision=prior["revision"],
                    idempotent=True,
                )
            if not work["current"]:
                raise Conflict("Formal source exit needs a current lease")
            self._current_attempt(conn, claim, operation_id, operation["attempt_id"])
            admission = self._admission_for(conn, operation["attempt_id"])

            # Success only accepts a reply the existing parser recognizes. The
            # material, including its time points, comes from that parser alone.
            try:
                if operation["action"] == "audio_transcription":
                    if (admission["state"] != "consumed" or operation["phase"] != "dispatching"
                            or operation["outcome"] != "none" or data["parse_version"] != ASR_PARSE_VERSION):
                        raise Conflict("ASR material needs its consumed dispatch and pinned parser")
                    material = parse_transcribed_reply(ref, raw_bytes, admission["session_proof"])
                else:
                    material = parse_reply(operation["action"], ref, raw_bytes)
            except SourceAccessError as exc:
                raise Conflict("Reply did not parse as source material") from exc
            if not material or not material.get("segments"):
                raise Conflict("Reply did not parse as source material")
            segments = [
                dict(SourceMaterialSegment(index=index, **segment).model_dump())
                for index, segment in enumerate(material["segments"])
            ]
            revision = claim.revision + 1
            material_id = uuid4()
            successor_id = uuid4()
            # The successor work row is written first so the material can
            # reference it; both live in this single transaction.
            conn.execute(
                text("""
                INSERT INTO work_items
                (work_id,task_id,contract_version,expected_revision,run_id,kind,state)
                VALUES (:work,:task,:version,:revision,:run,'build_artifact','ready')
                """),
                {
                    "work": successor_id,
                    "task": claim.task_id,
                    "version": claim.contract_version,
                    "revision": revision,
                    "run": claim.run_id,
                },
            )
            conn.execute(
                text("""
                INSERT INTO source_formal_materials
                (material_id,task_id,contract_version,revision,work_id,operation_id,attempt_id,
                 permission_decision_id,admission_id,owner_id,action,bvid,page,payload_hash,
                 adapter_version,parse_version,segments,material_state,successor_work_id,provenance)
                VALUES (:id,:task,:version,:revision,:work,:op,:att,:permission,:admission,
                        :owner,:action,:bvid,:page,:hash,:adapter,:parse,
                        CAST(:segments AS jsonb),'recorded',:successor,CAST(:provenance AS jsonb))
                """),
                {
                    "id": material_id,
                    "task": claim.task_id,
                    "version": claim.contract_version,
                    "revision": revision,
                    "work": claim.work_id,
                    "op": operation_id,
                    "att": operation["attempt_id"],
                    "permission": operation["permission_decision_id"],
                    "admission": admission["admission_id"],
                    "owner": allowed_owner,
                    "action": data["action"],
                    "bvid": data["bvid"],
                    "page": data["page"],
                    "hash": fingerprint,
                    "adapter": data["adapter_version"],
                    "parse": data["parse_version"],
                    "segments": encode(segments),
                    "provenance": encode(material.get("provenance", {})),
                    "successor": successor_id,
                },
            )
            conn.execute(
                text("""
                UPDATE source_reads SET phase='closed',outcome='succeeded',disposition='recorded',
                    error_code=NULL,raw_payload=:raw,payload_hash=:hash,
                    material=CAST(:material AS jsonb),received_at=clock_timestamp(),
                    outcome_basis=:basis,outcome_evidence=:evidence
                WHERE operation_id=:op
                """),
                {
                    "raw": raw_bytes,
                    "basis": "recorded_asr_candidate" if data["action"] == "audio_transcription"
                    else "verified_subtitle_reply",
                    "hash": fingerprint,
                    "material": encode(material),
                    "evidence": f"parse_reply({data['parse_version']}) returned material "
                    f"with {len(material) if hasattr(material, '__len__') else 1} element(s)",
                    "op": operation_id,
                },
            )
            conn.execute(
                text("UPDATE source_operation_attempts SET state='closed',"
                     "close_reason='result_recorded',closed_at=clock_timestamp() WHERE attempt_id=:att"),
                {"att": operation["attempt_id"]},
            )
            conn.execute(
                text("UPDATE work_items SET state='closed' WHERE work_id=:work"),
                {"work": claim.work_id},
            )
            conn.execute(
                text("UPDATE tasks SET revision=revision+1 WHERE task_id=:task"),
                {"task": claim.task_id},
            )
            self._transition(
                conn,
                claim.task_id,
                revision,
                "source_result_recorded",
                object_kind="source_material",
                object_ref=material_id,
                from_state="dispatching",
                to_state="recorded",
                reason_ref=operation_id,
                policy_version=source_policy(data["action"]),
                dedup_key=f"source-exit:{operation_id}",
            )
            return SourceSettlement(
                schema_version=1,
                disposition="recorded",
                material_id=material_id,
                payload_hash=fingerprint,
                segments=len(segments),
                successor_work_id=successor_id,
                expected_revision=revision,
                idempotent=False,
            )

    def _assert_not_stranded(self, conn, task_id, version):
        """A commit closing the last safe path may not strand an active Goal."""
        active = conn.execute(
            text("""
            SELECT count(*) FROM goals
            WHERE task_id=:task AND contract_version=:version AND required AND state='active'
            """),
            {"task": task_id, "version": version},
        ).scalar_one()
        if not active:
            return
        work = conn.execute(
            text("""
            SELECT count(*) FROM work_items
            WHERE task_id=:task AND contract_version=:version
              AND state IN ('ready','leased') AND kind IN ('form_goal','read_source')
            """),
            {"task": task_id, "version": version},
        ).scalar_one()
        waiting = conn.execute(
            text("""
            SELECT count(*) FROM goal_blockers
            WHERE task_id=:task AND contract_version=:version AND state='open'
            """),
            {"task": task_id, "version": version},
        ).scalar_one()
        if not work and not waiting:
            raise Conflict(
                "Refusing to close the last safe path: an active required Goal would be left "
                "with no executable work and no real waiting condition"
            )

    def settle_source_unknown(self, claim, allowed_owner, unknown):
        """Close an unknown outcome honestly, without retry and without 'no subtitles'.

        ``adapter_timeout``, a crash after possible dispatch and an identity
        contradiction are never read as a missing subtitle track. When the caller
        proves no legal path remains, the required Goals become ``indeterminate``
        in the same transaction; otherwise a real waiting/reconciliation path must
        survive the commit.
        """
        data = SourceUnknownInput.model_validate(unknown).model_dump()
        operation_id = UUID(data["operation_id"])
        with self.engine.begin() as conn:
            work = self._exit_scope(
                conn, claim, allowed_owner, "read_source", require_current=False
            )
            operation = self._operation_for_exit(conn, claim, operation_id)
            if operation["outcome"] in {"succeeded", "failed"}:
                raise Conflict("Source operation already has a settled outcome")
            if operation["phase"] == "closed" and operation["outcome"] == "outcome_unknown":
                return SourceUnknownClose(
                    schema_version=1,
                    disposition=operation["disposition"],
                    outcome="outcome_unknown",
                    closed_work=True,
                    goals_indeterminate=0,
                    expected_revision=claim.revision,
                )
            if not work["current"]:
                raise Conflict("Formal source exit needs a current lease")
            self._current_attempt(conn, claim, operation_id, operation["attempt_id"])
            exhausts = not data["remaining_legal_paths"]
            revision = claim.revision + 1
            indeterminate = 0
            conn.execute(
                text("""
                UPDATE source_reads SET phase='closed',outcome='outcome_unknown',
                    disposition='recorded',error_code=:code,raw_payload=NULL,payload_hash=NULL,
                    material=NULL,received_at=clock_timestamp(),
                    outcome_basis=:code,outcome_evidence=:evidence
                WHERE operation_id=:op
                """),
                {
                    "code": data["code"],
                    "evidence": data["evidence"],
                    "op": operation_id,
                },
            )
            conn.execute(
                text("UPDATE source_operation_attempts SET state='closed',"
                     "close_reason=:reason,closed_at=clock_timestamp() WHERE attempt_id=:att"),
                {"reason": f"outcome_unknown:{data['code']}", "att": operation["attempt_id"]},
            )
            if exhausts:
                indeterminate = conn.execute(
                    text("""
                    UPDATE goals SET state='indeterminate'
                    WHERE task_id=:task AND contract_version=:version
                      AND required AND state='active'
                    """),
                    {"task": claim.task_id, "version": claim.contract_version},
                ).rowcount
            else:
                # A real path survives: the active Goal must rest on a real
                # waiting condition, never on silence.
                conn.execute(
                    text("""
                    INSERT INTO goal_blockers
                    (blocker_id,task_id,contract_version,question,waiting_condition,
                     responsible_party,advanceable,exit_path,state)
                    VALUES (:id,:task,:version,:question,:waiting,:party,:advanceable,:exit,'open')
                    """),
                    {
                        "id": uuid4(),
                        "task": claim.task_id,
                        "version": claim.contract_version,
                        "question": "The admitted source operation ended without a settled outcome",
                        "waiting": data["waiting_condition"],
                        "party": "source_reconciliation",
                        "advanceable": True,
                        "exit": "Produce the authoritative per-operation result, or exhaust "
                        + "; ".join(data["remaining_legal_paths"]),
                    },
                )
            conn.execute(
                text("UPDATE work_items SET state='closed' WHERE work_id=:work"),
                {"work": claim.work_id},
            )
            conn.execute(
                text("UPDATE tasks SET revision=revision+1 WHERE task_id=:task"),
                {"task": claim.task_id},
            )
            self._transition(
                conn,
                claim.task_id,
                revision,
                "source_outcome_unknown",
                object_kind="source_operation",
                object_ref=operation_id,
                from_state="dispatching",
                to_state="outcome_unknown",
                reason_ref=operation["attempt_id"],
                policy_version=source_policy(operation["action"]),
                dedup_key=f"source-unknown:{operation_id}",
            )
            self._assert_not_stranded(conn, claim.task_id, claim.contract_version)
            return SourceUnknownClose(
                schema_version=1,
                disposition="recorded",
                outcome="outcome_unknown",
                closed_work=True,
                goals_indeterminate=indeterminate,
                expected_revision=revision,
            )

    def settle_source_failure(self, claim, allowed_owner, operation_id, code, evidence):
        """Record a real, explicit source failure without calling it 'no subtitles'."""
        if not code or not code.strip() or not evidence or not evidence.strip():
            raise ValueError("A real failure code and its evidence are required")
        operation_id = UUID(operation_id) if not isinstance(operation_id, UUID) else operation_id
        with self.engine.begin() as conn:
            work = self._exit_scope(
                conn, claim, allowed_owner, "read_source", require_current=False
            )
            operation = self._operation_for_exit(conn, claim, operation_id)
            if operation["outcome"] in {"succeeded", "outcome_unknown"}:
                raise Conflict("Source operation already has a settled outcome")
            if operation["phase"] == "closed" and operation["outcome"] == "failed":
                # The identical failure re-send must not advance the revision
                # twice; a different error story is a conflict.
                if operation["error_code"] != code:
                    raise Conflict("Immutable source failure cannot be changed")
                return SourceUnknownClose(
                    schema_version=1,
                    disposition=operation["disposition"],
                    outcome="failed",
                    closed_work=True,
                    goals_indeterminate=0,
                    expected_revision=claim.revision,
                )
            if not work["current"]:
                raise Conflict("Formal source exit needs a current lease")
            self._current_attempt(conn, claim, operation_id, operation["attempt_id"])
            revision = claim.revision + 1
            conn.execute(
                text("""
                UPDATE source_reads SET phase='closed',outcome='failed',disposition='recorded',
                    error_code=:code,raw_payload=NULL,payload_hash=NULL,material=NULL,
                    received_at=clock_timestamp(),outcome_basis=:code,outcome_evidence=:evidence
                WHERE operation_id=:op
                """),
                {"code": code, "evidence": evidence, "op": operation_id},
            )
            conn.execute(
                text("UPDATE source_operation_attempts SET state='closed',"
                     "close_reason=:reason,closed_at=clock_timestamp() WHERE attempt_id=:att"),
                {"reason": f"failed:{code}", "att": operation["attempt_id"]},
            )
            # A real, explicit failure of the admitted source path: the required
            # Goals cannot be satisfied by it and are closed honestly. This never
            # claims the video has no subtitles.
            indeterminate = conn.execute(
                text("""
                UPDATE goals SET state='indeterminate'
                WHERE task_id=:task AND contract_version=:version
                  AND required AND state='active'
                """),
                {"task": claim.task_id, "version": claim.contract_version},
            ).rowcount
            conn.execute(
                text("UPDATE work_items SET state='closed' WHERE work_id=:work"),
                {"work": claim.work_id},
            )
            conn.execute(
                text("UPDATE tasks SET revision=revision+1 WHERE task_id=:task"),
                {"task": claim.task_id},
            )
            self._transition(
                conn,
                claim.task_id,
                revision,
                "source_result_failed",
                object_kind="source_operation",
                object_ref=operation_id,
                from_state="dispatching",
                to_state="failed",
                reason_ref=operation["attempt_id"],
                policy_version=source_policy(operation["action"]),
                dedup_key=f"source-failed:{operation_id}",
            )
            self._assert_not_stranded(conn, claim.task_id, claim.contract_version)
            return SourceUnknownClose(
                schema_version=1,
                disposition="recorded",
                outcome="failed",
                closed_work=True,
                goals_indeterminate=indeterminate,
                expected_revision=revision,
            )

    def read_formal_materials(self, owner, task_id):
        """Formal material for the accepted contract, separated from diagnostics.

        Filtered by owner, current contract version, permission causality and the
        ``recorded``+``succeeded`` states. The diagnostic ``forming`` query in
        ``SourceRuntime.current`` is never widened by this interface.
        """
        if not owner:
            raise ValueError("Task owner scope required")
        with self.engine.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    text("""
                SELECT m.material_id,m.operation_id,m.attempt_id,m.permission_decision_id,
                       m.action,m.bvid,m.page,m.payload_hash,m.adapter_version,m.parse_version,
                       m.segments,m.provenance,m.material_state,m.successor_work_id,m.revision,m.recorded_at
                FROM source_formal_materials m
                JOIN tasks t ON t.task_id=m.task_id
                JOIN task_contracts c ON c.task_id=t.task_id AND c.version=t.current_version
                JOIN source_reads r ON r.operation_id=m.operation_id
                WHERE t.task_id=:task AND t.owner_id=:owner
                  AND m.contract_version=t.current_version AND m.owner_id=:owner
                  AND m.material_state='recorded'
                  AND r.outcome='succeeded' AND r.disposition='recorded'
                  AND r.permission_decision_id=m.permission_decision_id
                ORDER BY m.recorded_at,m.material_id
                """),
                    {"task": task_id, "owner": owner},
                ).mappings()
            ]

    def save_artifact_candidate(self, claim, allowed_owner, draft):
        """Commit one source-bound candidate; never certify quality or completion."""
        from .artifact_builder import ArtifactBuildError, render_markdown, validate_method_list
        from .artifact_quality import draft_sha256

        if not allowed_owner:
            raise ValueError("Task owner scope required")
        with self.engine.begin() as conn:
            work, current = self._locked_claim(conn, claim)
            if work["task_owner_id"] != allowed_owner or work["kind"] != "build_artifact":
                raise Conflict("Artifact lease or owner mismatch")
            if digest(claim.request) != digest(work["claim_contract_input"]):
                raise Conflict("Artifact input fingerprint mismatch")
            material = conn.execute(
                text("""
                SELECT m.* FROM source_formal_materials m
                JOIN source_reads r ON r.operation_id=m.operation_id
                WHERE m.successor_work_id=:work AND m.task_id=:task
                  AND m.contract_version=:version AND m.owner_id=:owner
                  AND m.material_state='recorded' AND m.action IN ('subtitle','audio_transcription')
                  AND r.outcome='succeeded' AND r.disposition='recorded'
                  AND r.permission_decision_id=m.permission_decision_id
                """),
                {"work": claim.work_id, "task": claim.task_id,
                 "version": claim.contract_version, "owner": allowed_owner},
            ).mappings().one_or_none()
            if material is None:
                raise Conflict("No authoritative subtitle material for artifact work")
            try:
                checked = validate_method_list(
                    draft, material["segments"], work["claim_contract_input"]["message"],
                    material_source_ref(material),
                    material["payload_hash"],
                )
                markdown = render_markdown(checked)
            except ArtifactBuildError as exc:
                raise Conflict(f"Artifact candidate rejected: {exc.code}") from exc
            fingerprint = draft_sha256(checked)
            prior = conn.execute(
                text("SELECT artifact_id,draft_hash,state FROM artifact_candidates WHERE work_id=:work"),
                {"work": claim.work_id},
            ).mappings().one_or_none()
            if prior is not None:
                if prior["draft_hash"] != fingerprint:
                    raise Conflict("Immutable artifact candidate cannot be changed")
                return {"artifact_id": str(prior["artifact_id"]), "state": prior["state"],
                        "idempotent": True}
            if not current or work["task_contract_state"] != "accepted":
                raise Conflict("Artifact candidate needs a current accepted lease")
            artifact_id, receipt_id = uuid4(), uuid4()
            revision = claim.revision + 1
            conn.execute(
                text("""
                INSERT INTO artifact_candidates
                (artifact_id,task_id,contract_version,revision,work_id,material_id,
                 owner_id,payload_hash,draft_hash,draft,markdown,state)
                VALUES (:id,:task,:version,:revision,:work,:material,:owner,:payload_hash,
                        :draft_hash,CAST(:draft AS jsonb),:markdown,'unreviewed_candidate')
                """),
                {"id": artifact_id, "task": claim.task_id, "version": claim.contract_version,
                 "revision": revision, "work": claim.work_id, "material": material["material_id"],
                 "owner": allowed_owner, "payload_hash": material["payload_hash"],
                 "draft_hash": fingerprint, "draft": encode(checked), "markdown": markdown},
            )
            receipt = {"schema_version": 1, "stage": "artifact_candidate",
                       "artifact_id": str(artifact_id), "draft_hash": fingerprint}
            conn.execute(
                text("""
                INSERT INTO work_receipts
                (receipt_id,lease_token,work_id,disposition,outcome,payload,payload_hash)
                VALUES (:id,:token,:work,'recorded','ok',CAST(:payload AS jsonb),:hash)
                """),
                {"id": receipt_id, "token": claim.token, "work": claim.work_id,
                 "payload": encode(receipt), "hash": digest(receipt)},
            )
            conn.execute(
                text("UPDATE work_items SET state='closed',receipt_id=:receipt WHERE work_id=:work"),
                {"receipt": receipt_id, "work": claim.work_id},
            )
            conn.execute(
                text("""
                INSERT INTO goal_blockers
                (blocker_id,task_id,contract_version,question,waiting_condition,
                 responsible_party,advanceable,exit_path,state)
                VALUES (:id,:task,:version,:question,:waiting,'task_owner',true,:exit,'open')
                """),
                {"id": uuid4(), "task": claim.task_id, "version": claim.contract_version,
                 "question": "请核对候选方法清单及字幕证据，确认是否满足原请求。",
                 "waiting": "等待任务所有者验收候选成果；当前软件界面仍需独立确认。",
                 "exit": "确认成果或修订任务后继续。"},
            )
            conn.execute(text("UPDATE tasks SET revision=revision+1 WHERE task_id=:task"),
                         {"task": claim.task_id})
            self._transition(
                conn, claim.task_id, revision, "artifact_candidate_saved",
                object_kind="artifact", object_ref=artifact_id,
                from_state="building", to_state="unreviewed_candidate",
                reason_ref=material["material_id"], dedup_key=f"artifact:{claim.work_id}",
            )
            self._assert_not_stranded(conn, claim.task_id, claim.contract_version)
            return {"artifact_id": str(artifact_id), "state": "unreviewed_candidate",
                    "idempotent": False}

    def release_artifact_claim(self, claim, allowed_owner, error_code):
        """Release a failed model attempt; no candidate exists and retry is explicit."""
        if not allowed_owner or not error_code or not error_code.strip():
            raise ValueError("Owner and stable error code required")
        with self.engine.begin() as conn:
            work, current = self._locked_claim(conn, claim)
            if (
                work["task_owner_id"] != allowed_owner or work["kind"] != "build_artifact"
                or not current
            ):
                raise Conflict("Only the live artifact lease can be released")
            exists = conn.execute(
                text("SELECT 1 FROM artifact_candidates WHERE work_id=:work"),
                {"work": claim.work_id},
            ).scalar_one_or_none()
            if exists:
                raise Conflict("A saved artifact cannot be released for retry")
            payload = {"schema_version": 1, "stage": "artifact_build_failed",
                       "error_code": error_code}
            conn.execute(
                text("""
                INSERT INTO work_receipts
                (receipt_id,lease_token,work_id,disposition,outcome,payload,payload_hash)
                VALUES (:id,:token,:work,'recorded','error',CAST(:payload AS jsonb),:hash)
                """),
                {"id": uuid4(), "token": claim.token, "work": claim.work_id,
                 "payload": encode(payload), "hash": digest(payload)},
            )
            conn.execute(
                text("""
                UPDATE work_items SET state='ready',lease_token=NULL,lease_owner=NULL,
                    lease_until=NULL,available_at=clock_timestamp()
                WHERE work_id=:work
                """),
                {"work": claim.work_id},
            )
            return {"state": "ready", "error_code": error_code}

    def recover_artifact(self, claim, allowed_owner):
        """After lease expiry, restore artifact work only if no draft was committed."""
        if claim.mode != "reconcile" or not allowed_owner:
            raise Conflict("Artifact recovery needs an owner-scoped reconcile lease")
        with self.engine.begin() as conn:
            work, current = self._locked_claim(conn, claim)
            if (
                not current or work["kind"] != "build_artifact"
                or work["task_owner_id"] != allowed_owner
            ):
                raise Conflict("Artifact recovery lease is no longer current")
            if conn.execute(
                text("SELECT 1 FROM artifact_candidates WHERE work_id=:work"),
                {"work": claim.work_id},
            ).scalar_one_or_none():
                raise Conflict("Artifact already exists")
            payload = {"schema_version": 1, "stage": "artifact_attempt_interrupted"}
            conn.execute(
                text("""
                INSERT INTO work_receipts
                (receipt_id,lease_token,work_id,disposition,outcome,payload,payload_hash)
                VALUES (:id,:token,:work,'recorded','error',CAST(:payload AS jsonb),:hash)
                """),
                {"id": uuid4(), "token": claim.token, "work": claim.work_id,
                 "payload": encode(payload), "hash": digest(payload)},
            )
            conn.execute(
                text("""
                UPDATE work_items SET state='ready',lease_token=NULL,lease_owner=NULL,
                    lease_until=NULL,available_at=clock_timestamp()
                WHERE work_id=:work
                """),
                {"work": claim.work_id},
            )
            return {"state": "ready"}

    def save_artifact_review(self, owner, task_id, artifact_id, expected_revision, review):
        """Append one validated advisory review without changing task completion."""
        from .artifact_quality import (
            ArtifactReviewError,
            draft_sha256,
            validate_review_candidate,
        )

        if not owner:
            raise ValueError("Task owner scope required")
        artifact_id = UUID(str(artifact_id))
        with self.engine.begin() as conn:
            view = self._view(conn, owner, task_id, lock=True)
            artifact = conn.execute(
                text("""
                SELECT a.*,c.input AS contract_input FROM artifact_candidates a
                JOIN task_contracts c ON c.task_id=a.task_id
                  AND c.version=a.contract_version
                WHERE a.artifact_id=:artifact AND a.task_id=:task AND a.owner_id=:owner
                FOR UPDATE OF a
                """),
                {"artifact": artifact_id, "task": task_id, "owner": owner},
            ).mappings().one_or_none()
            if artifact is None or artifact["contract_version"] != view["current_version"]:
                raise NotFound()
            if (view["revision"] != expected_revision
                    or view["contract_state"] != "accepted"
                    or view["completed"]):
                raise Conflict("Review candidate revision or contract changed")
            material = conn.execute(
                text("""
                SELECT m.* FROM source_formal_materials m
                JOIN source_reads r ON r.operation_id=m.operation_id
                WHERE m.material_id=:material AND m.task_id=:task
                  AND m.contract_version=:version AND m.owner_id=:owner
                  AND m.material_state='recorded' AND m.action IN ('subtitle','audio_transcription')
                  AND r.outcome='succeeded' AND r.disposition='recorded'
                  AND r.permission_decision_id=m.permission_decision_id
                """),
                {"material": artifact["material_id"], "task": task_id,
                 "version": view["current_version"], "owner": owner},
            ).mappings().one_or_none()
            if material is None or material["payload_hash"] != artifact["payload_hash"]:
                raise Conflict("Review has no current formal material")
            if draft_sha256(artifact["draft"]) != artifact["draft_hash"]:
                raise Conflict("Stored artifact draft hash changed")
            try:
                checked = validate_review_candidate(
                    review, artifact["draft"], material["segments"],
                    artifact["contract_input"]["message"],
                    material_source_ref(material),
                    material["payload_hash"],
                )
            except ArtifactReviewError as exc:
                raise Conflict(f"Advisory review rejected: {exc.code}") from exc
            fingerprint = digest(checked)
            prior = conn.execute(
                text("""
                SELECT review_id FROM artifact_review_candidates
                WHERE artifact_id=:artifact AND review_hash=:hash
                """),
                {"artifact": artifact_id, "hash": fingerprint},
            ).scalar_one_or_none()
            if prior is not None:
                return {"review_id": str(prior), "idempotent": True}
            review_id = uuid4()
            conn.execute(
                text("""
                INSERT INTO artifact_review_candidates
                (review_id,artifact_id,draft_hash,payload_hash,reviewer_model_id,
                 review_hash,review)
                VALUES (:id,:artifact,:draft_hash,:payload_hash,:reviewer,:hash,
                        CAST(:review AS jsonb))
                """),
                {"id": review_id, "artifact": artifact_id,
                 "draft_hash": artifact["draft_hash"],
                 "payload_hash": material["payload_hash"],
                 "reviewer": checked["reviewer_model_id"],
                 "hash": fingerprint, "review": encode(checked)},
            )
            return {"review_id": str(review_id), "idempotent": False}

    def get_artifact(self, owner, task_id):
        """Current-version candidate only; owner isolation also hides prior versions."""
        if not owner:
            raise ValueError("Task owner scope required")
        with self.engine.connect() as conn:
            row = conn.execute(
                text("""
                SELECT a.artifact_id,a.revision,a.material_id,a.draft,a.markdown,
                       a.state,a.draft_hash,a.payload_hash,a.created_at
                FROM artifact_candidates a JOIN tasks t ON t.task_id=a.task_id
                WHERE a.task_id=:task AND t.owner_id=:owner AND a.owner_id=:owner
                  AND a.contract_version=t.current_version
                ORDER BY a.created_at DESC,a.artifact_id DESC LIMIT 1
                """),
                {"task": task_id, "owner": owner},
            ).mappings().one_or_none()
            if row is None:
                raise NotFound()
            accepted = conn.execute(
                text("""
                SELECT 1 FROM transitions WHERE task_id=:task
                  AND kind='artifact_owner_accepted' AND object_ref=:artifact
                """),
                {"task": task_id, "artifact": row["artifact_id"]},
            ).scalar_one_or_none()
            reviews = [
                {**dict(item), "review_id": str(item["review_id"])}
                for item in conn.execute(
                    text("""
                    SELECT review_id,reviewer_model_id,review_hash,review,created_at
                    FROM artifact_review_candidates WHERE artifact_id=:artifact
                    ORDER BY created_at,review_id
                    """),
                    {"artifact": row["artifact_id"]},
                ).mappings()
            ]
            return {**dict(row), "artifact_id": str(row["artifact_id"]),
                    "material_id": str(row["material_id"]),
                    "owner_accepted": bool(accepted), "reviews": reviews}

    def accept_artifact(self, owner, task_id, artifact_id, expected_revision):
        """The authenticated task owner explicitly accepts the visible candidate."""
        if not owner:
            raise ValueError("Task owner required")
        artifact_id = UUID(str(artifact_id))
        with self.engine.begin() as conn:
            view = self._view(conn, owner, task_id, lock=True)
            artifact = conn.execute(
                text("""
                SELECT a.artifact_id,a.contract_version,a.work_id,a.material_id
                FROM artifact_candidates a
                WHERE a.artifact_id=:artifact AND a.task_id=:task AND a.owner_id=:owner
                FOR UPDATE
                """),
                {"artifact": artifact_id, "task": task_id, "owner": owner},
            ).mappings().one_or_none()
            if artifact is None or artifact["contract_version"] != view["current_version"]:
                raise NotFound()
            prior = conn.execute(
                text("""
                SELECT 1 FROM transitions WHERE task_id=:task
                  AND kind='artifact_owner_accepted' AND object_ref=:artifact
                """),
                {"task": task_id, "artifact": artifact_id},
            ).scalar_one_or_none()
            if prior:
                return {"artifact_id": str(artifact_id), "completed": view["completed"],
                        "idempotent": True}
            if view["revision"] != expected_revision or view["contract_state"] != "accepted":
                raise Conflict("Artifact acceptance revision or contract changed")
            active = conn.execute(
                text("""
                SELECT count(*) FROM goals WHERE task_id=:task AND contract_version=:version
                  AND required AND state='active'
                """),
                {"task": task_id, "version": view["current_version"]},
            ).scalar_one()
            if not active:
                raise Conflict("No active required goals to accept")
            conn.execute(
                text("""
                UPDATE goals SET state='satisfied',progress='complete'
                WHERE task_id=:task AND contract_version=:version
                  AND required AND state='active'
                """),
                {"task": task_id, "version": view["current_version"]},
            )
            self._end_blockers(conn, task_id, view["current_version"], "resolved")
            revision = expected_revision + 1
            conn.execute(
                text("UPDATE tasks SET revision=:revision WHERE task_id=:task"),
                {"revision": revision, "task": task_id},
            )
            self._transition(
                conn, task_id, revision, "artifact_owner_accepted",
                object_kind="artifact_candidate", object_ref=artifact_id,
                from_state="unreviewed_candidate", to_state="owner_accepted",
                reason_ref=artifact["material_id"], policy_version="explicit-owner-accept-v1",
                dedup_key=f"owner-accept:{artifact_id}",
            )
            return {"artifact_id": str(artifact_id), "completed": True, "idempotent": False}
