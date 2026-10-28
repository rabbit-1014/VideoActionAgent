"""Internal-handler runner only; external tools require the later admission path."""

import argparse
import json
import logging
import os
import time
from uuid import uuid4

from sqlalchemy import create_engine

from .runtime import Runtime

log = logging.getLogger("video_worker")


class Worker:
    def __init__(self, runtime, worker_id, handlers):
        self.runtime = runtime
        self.worker_id = worker_id
        self.handlers = dict(handlers)

    def run_once(self):
        claim = self.runtime.claim(self.worker_id, set(self.handlers))
        if claim is None:
            return False
        started = time.monotonic()
        outcome = "ok"
        try:
            payload = self.handlers[claim.kind](claim)
        except Exception:  # noqa: BLE001 - internal callback boundary; details may contain secrets
            # Persist a safe diagnostic code, not credentials embedded in exception text.
            payload = {"schema_version": 1, "error_code": "internal_handler_error"}
            outcome = "error"
        disposition = self.runtime.record(claim, payload, outcome)
        log.info(
            json.dumps(
                {
                    "event": "work_recorded",
                    "task_id": str(claim.task_id),
                    "run_id": str(claim.run_id),
                    "trace_id": str(claim.trace_id),
                    "span_id": uuid4().hex,
                    "work_id": str(claim.work_id),
                    "outcome": outcome,
                    "disposition": disposition,
                    "duration_seconds": time.monotonic() - started,
                }
            )
        )
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"], pool_pre_ping=True)
    # No production handler exists at I1. Never fabricate video/goal output.
    worker = Worker(Runtime(engine), uuid4().hex, {})
    log.info(json.dumps({"event": "worker_started", "handler_count": 0}))
    try:
        while True:
            processed = worker.run_once()
            if args.once:
                return
            if not processed:
                time.sleep(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
