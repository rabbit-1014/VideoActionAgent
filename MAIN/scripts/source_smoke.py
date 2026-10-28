"""Explicit local-development probe; does not publish or complete a product task."""

import argparse
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from video_action_agent.runtime import Runtime
from video_action_agent.source_access import LocalOpenCli
from video_action_agent.source_runtime import SourceRuntime
from video_action_agent.source_worker import run_once


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-browser", action="store_true", required=True)
    parser.add_argument("--opencli-entry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--navigation-workaround",
        action="store_true",
        help="Opt in to OpenCLI trace-based navigation workaround; local failure traces may be saved.",
    )
    args = parser.parse_args()
    adapter = LocalOpenCli(
        shutil.which("node"), args.opencli_entry, navigation_workaround=args.navigation_workaround
    )
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"])
    try:
        runtime = Runtime(engine, lease_seconds=300)
        owner = "local-source-probe-" + uuid4().hex
        task = runtime.submit(
            owner,
            "probe",
            {"message": "Inspect this video source and subtitles", "source": "BV1gD421u75J"},
        )
        service = SourceRuntime(runtime, owner, args.allow_local_browser)
        result = run_once(service, adapter, "local-source-inspector", task["task_id"])
        with engine.connect() as conn:
            rows = (
                conn.execute(
                    text("""
                SELECT action,phase,outcome,disposition,error_code,payload_hash,
                       octet_length(raw_payload) AS bytes,material->>'kind' AS material_kind
                FROM source_reads WHERE task_id=:task ORDER BY created_at
            """),
                    {"task": task["task_id"]},
                )
                .mappings()
                .all()
            )
        report = {
            "at": datetime.now(UTC).isoformat(),
            "mode": "live_local_browser",
            "navigation_workaround": args.navigation_workaround,
            "bvid": "BV1gD421u75J",
            "result": result,
            "stored_reads": [dict(r) for r in rows],
            "contract_state": runtime.get(owner, task["task_id"])["contract_state"],
            "limitation": "Local desktop source probe, no generated product artifact.",
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
