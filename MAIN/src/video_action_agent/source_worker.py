"""Explicit local-browser inspection; never the default or public-server worker.

This entry point is a diagnostic probe: it may only inspect tasks its own run
submitted under the reserved ``local-source-probe-`` owner namespace, and only the
task it names. It can never consume another task's formation work.
"""

import argparse
import json
import os
import shutil
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import create_engine

from .runtime import Conflict, Runtime
from .source_access import MAX_REPLY_BYTES, LocalOpenCli, SourceAccessError
from .source_runtime import SourceRuntime

# Held by the service and the probe script, never by a user request field.
DIAGNOSTIC_OWNER_PREFIX = "local-source-probe-"


def is_diagnostic_owner(owner):
    return (
        isinstance(owner, str)
        and owner.startswith(DIAGNOSTIC_OWNER_PREFIX)
        and len(owner) > len(DIAGNOSTIC_OWNER_PREFIX)
    )


def _diagnostic_owner_arg(owner):
    if not is_diagnostic_owner(owner):
        raise argparse.ArgumentTypeError(
            f"owner must be in the reserved {DIAGNOSTIC_OWNER_PREFIX}* namespace"
        )
    return owner


def run_once(service, adapter, worker_id, task_id=None, actions=("video", "subtitle")):
    if not service.browser_authorized or not service.allowed_owner:
        raise Conflict("Explicit local browser authorization and owner required")
    if not is_diagnostic_owner(service.allowed_owner):
        raise Conflict("Source inspection is reserved for diagnostic probe owners")
    if not actions or any(action not in {"video", "subtitle"} for action in actions):
        raise ValueError("Registered source reads only")
    if task_id is None:
        return {
            "processed": False,
            "task_id": None,
            "reason": "target_task_required",
            "goal_accepted": False,
            "product_completed": False,
        }
    runtime = service.runtime
    claim = runtime.claim(
        worker_id, {"form_goal"}, allowed_owner=service.allowed_owner, task_id=task_id
    )
    if claim is None:
        return {
            "processed": False,
            "task_id": str(task_id),
            "reason": "target_task_not_claimable",
            "goal_accepted": False,
            "product_completed": False,
        }
    reads = []
    for action in actions:
        operation = None
        raw = None
        try:
            if not runtime.renew(claim):
                raise Conflict("Task changed before source read")
            operation = service.admit(claim, action)
            ref = service.begin(claim, operation)
            if ref is None:
                # A consumed dispatch is not a new permission to call the browser.
                reads.append({"action": action, "outcome": "requires_reconciliation"})
                break
            raw = adapter.fetch(action, ref)
            disposition = service.finish(claim, operation, raw)
            reads.append(
                {
                    "action": action,
                    "operation_id": str(operation),
                    "outcome": "succeeded",
                    "disposition": disposition,
                }
            )
            if disposition == "stale":
                break
        except SourceAccessError as exc:
            code = str(exc)
            if operation is not None:
                retained = raw if raw is not None and len(raw) <= MAX_REPLY_BYTES else None
                disposition = service.fail(claim, operation, code, raw=retained)
            else:
                disposition = "not_dispatched"
            reads.append(
                {
                    "action": action,
                    "outcome": "outcome_unknown" if code == "adapter_timeout" else "failed",
                    "code": code,
                    "disposition": disposition,
                }
            )
            break
        except Conflict:
            reads.append({"action": action, "outcome": "not_dispatched_or_stale"})
            break
    payload = {"schema_version": 1, "stage": "source_inspected_goal_unresolved", "reads": reads}
    outcome = "ok" if all(r["outcome"] == "succeeded" for r in reads) else "error"
    disposition = runtime.record(claim, payload, outcome)
    return {
        "processed": True,
        "task_id": str(claim.task_id),
        "reads": reads,
        "work_receipt": disposition,
        "goal_accepted": False,
        "product_completed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", type=_diagnostic_owner_arg, required=True)
    parser.add_argument(
        "--task-id",
        type=UUID,
        required=True,
        help="Diagnostic target task created by this probe run; never another task",
    )
    parser.add_argument("--allow-local-browser", action="store_true", required=True)
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument("--opencli-entry", type=Path, required=True)
    parser.add_argument(
        "--navigation-workaround",
        action="store_true",
        help="Opt in to OpenCLI trace-based navigation workaround; local failure traces may be saved.",
    )
    args = parser.parse_args()
    node = shutil.which("node")
    if not node:
        raise SystemExit("Node runtime unavailable")
    adapter = LocalOpenCli(
        node, args.opencli_entry, navigation_workaround=args.navigation_workaround
    )
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"], pool_pre_ping=True)
    try:
        service = SourceRuntime(
            Runtime(engine, lease_seconds=300), args.owner, args.allow_local_browser
        )
        result = run_once(
            service,
            adapter,
            uuid4().hex,
            args.task_id,
            ("video",) if args.metadata_only else ("video", "subtitle"),
        )
        print(json.dumps(result, ensure_ascii=True))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
