"""Generate or read a local saved-source notes candidate; no source recapture."""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from video_action_agent.notes_service import NotesService
from video_action_agent.source_candidate_bundle import read_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-bundle", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--request")
    parser.add_argument("--allow-model-text", action="store_true")
    parser.add_argument("--read")
    parser.add_argument("--output-kind", choices=("structured_notes", "method_list"),
                        default="structured_notes")
    parser.add_argument("--provider", choices=("zhipu", "deepseek"), default="zhipu")
    parser.add_argument("--direct-zhipu", action="store_true")
    parser.add_argument("--zhipu-model", default="glm-5.3")
    parser.add_argument("--zhipu-effort", choices=("low", "high", "max"))
    parser.add_argument("--stream-zhipu", action="store_true")
    args = parser.parse_args(argv)
    if not args.read and (not args.allow_model_text or not args.source_bundle or not args.request):
        parser.error("Generation requires source, request and explicit model text permission")
    from video_action_agent.deepseek_provider import DeepSeekProvider
    from video_action_agent.zhipu_provider import ZhipuProvider

    def factory():
        return (ZhipuProvider(args.zhipu_model, timeout_seconds=180,
                             use_system_proxy=not args.direct_zhipu,
                             stream=args.stream_zhipu, reasoning_effort=args.zhipu_effort)
                if args.provider == "zhipu" else DeepSeekProvider())

    manager = NotesService(args.owner, args.output_root, factory,
                           saved_bundle=read_bundle(args.source_bundle)
                           if args.source_bundle else None)
    try:
        if args.read:
            job_id = args.read
        else:
            job = manager.start(uuid4(), "saved", args.request, allow_model_text=True,
                                output_kind=args.output_kind)
            job_id = job["job_id"]
            end = time.monotonic() + 200
            while manager.get(job_id)["state"] in {"queued", "running"} and time.monotonic() < end:
                manager.wait(job_id, 1)
        job = manager.get(job_id)
        if job["state"] != "available":
            print(json.dumps(job, ensure_ascii=False))
            return 1
        result = manager.candidate(job_id)
        draft = result["candidate"]
        print(json.dumps({**job, "items": len(draft.get("sections", draft.get("methods", []))),
                          "model_id": result["candidate"]["model_id"]}, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001 -- never print upstream response/credentials
        print(json.dumps({"error_code": getattr(exc, "code", "notes_failed")}))
        return 1
    finally:
        manager.close()


if __name__ == "__main__":
    raise SystemExit(main())
