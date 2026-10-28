"""Launch a loopback-only task interface with a fresh in-memory access session."""

import argparse
import json
import os
import secrets
import shutil
import threading
import webbrowser
from pathlib import Path

import uvicorn
from sqlalchemy import create_engine

from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.notes_checks import NotesChecks
from video_action_agent.notes_service import NotesService
from video_action_agent.processing_observations import ProcessingObservations
from video_action_agent.runtime import Runtime
from video_action_agent.source_candidate_bundle import read_bundle
from video_action_agent.source_capture import SourceCapture
from video_action_agent.source_preview import SourcePreviewError, load_source_preview
from video_action_agent.ui_processing import UIProcessingError, build_ui_worker


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--allow-local-browser", action="store_true")
    parser.add_argument("--allow-local-audio-read", action="store_true")
    parser.add_argument("--source-bundle", type=Path)
    parser.add_argument("--allow-source-capture", action="store_true")
    parser.add_argument("--allow-notes", action="store_true")
    parser.add_argument("--allow-notes-checks", action="store_true",
                        help="Explicit independent glm-5.3/high/direct-stream opinion service")
    parser.add_argument("--fixed-subtitle-reference", action="store_true",
                        help="Read-only independent reference pinned to the existing saved v7")
    parser.add_argument("--notes-job", help="Restore an owner-bound saved notes job UUID")
    parser.add_argument("--notes-output-root", type=Path,
                        default=Path("D:/VideoActionAgent-asr-pilot/notes-bundles"))
    parser.add_argument("--source-output-root", type=Path,
                        default=Path("D:/VideoActionAgent-asr-pilot/source-bundles"))
    parser.add_argument("--opencli-entry", type=Path)
    parser.add_argument("--navigation-workaround", action="store_true")
    parser.add_argument("--provider", choices=("zhipu", "deepseek"), default="zhipu")
    parser.add_argument("--direct-zhipu", action="store_true",
                        help="Use direct verified TLS for local Zhipu draft generation")
    parser.add_argument("--zhipu-model", default="glm-5.3")
    parser.add_argument("--zhipu-effort", choices=("low", "high", "max"))
    parser.add_argument("--stream-zhipu", action="store_true")
    args = parser.parse_args(argv)
    if args.allow_notes_checks and not args.allow_notes:
        parser.error("Notes checks require --allow-notes")
    if args.fixed_subtitle_reference and not args.allow_notes:
        parser.error("Fixed reference requires --allow-notes")
    if not 1024 <= args.port <= 65535:
        parser.error("Use a local port from 1024 through 65535")
    node = shutil.which("node") if args.allow_local_browser else None
    if args.allow_local_browser and (
        node is None or args.opencli_entry is None or not args.opencli_entry.is_file()
    ):
        parser.error("Browser execution requires Node.js and a local --opencli-entry")
    if not os.environ.get("VIDEO_DATABASE_URL") or not os.environ.get("VIDEO_OWNER_ID"):
        parser.error("Load scripts/local-env.ps1 before starting the local interface")

    owner = os.environ["VIDEO_OWNER_ID"]
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"], pool_pre_ping=True)
    runtime = Runtime(engine, lease_seconds=300)
    session_token = secrets.token_urlsafe(32)
    stop = threading.Event()
    worker_state = {"running": False}
    observations = ProcessingObservations()
    worker_thread = None
    source_capture = None
    notes_service = None
    notes_checks = None
    source_modes = tuple(mode for mode, enabled in (
        ("subtitle", args.allow_local_browser),
        ("audio_transcription", args.allow_local_audio_read),
    ) if enabled)
    try:
        source_preview = load_source_preview(args.source_bundle) if args.source_bundle else None
        if args.allow_source_capture:
            from video_action_agent.audio_session import WORKER_PYTHON, WORKER_SCRIPT
            from video_action_agent.local_transcriber import PILOT_ROOT

            if not all(path.is_file() for path in (
                WORKER_PYTHON, WORKER_SCRIPT, PILOT_ROOT / "venv/Scripts/python.exe",
                Path(__file__).resolve().parent / "asr_transcribe.py",
            )):
                raise UIProcessingError("source_capture_runtime_unavailable")
            source_capture = SourceCapture(owner, args.source_output_root)
        if args.allow_notes:
            from video_action_agent.deepseek_provider import DeepSeekProvider
            from video_action_agent.zhipu_provider import ZhipuProvider

            provider_factory = (lambda: ZhipuProvider(args.zhipu_model, timeout_seconds=180,
                                                     use_system_proxy=not args.direct_zhipu,
                                                     stream=args.stream_zhipu,
                                                     reasoning_effort=args.zhipu_effort)
                                if args.provider == "zhipu" else DeepSeekProvider())
            notes_service = NotesService(owner, args.notes_output_root, provider_factory,
                                         saved_bundle=read_bundle(args.source_bundle)
                                         if args.source_bundle else None,
                                         source_capture=source_capture)
            if args.notes_job:
                notes_service.get(args.notes_job)
            if args.allow_notes_checks:
                notes_checks = NotesChecks(notes_service, lambda: ZhipuProvider(
                    "glm-5.3", timeout_seconds=300, use_system_proxy=False,
                    stream=True, reasoning_effort="high"))
        if source_modes:
            worker = build_ui_worker(
                runtime, owner, source_modes, provider_name=args.provider,
                node=node, opencli_entry=args.opencli_entry,
                navigation_workaround=args.navigation_workaround,
            )

            def execute():
                worker_state["running"] = True
                try:
                    while not stop.is_set():
                        worker.run_cycle(on_result=observations.record)
                        stop.wait(5)
                except Exception:  # noqa: BLE001 -- no raw provider/database errors in local output
                    print(json.dumps({"event": "processing_service_stopped"}), flush=True)
                finally:
                    worker_state["running"] = False

            worker_thread = threading.Thread(target=execute, daemon=True)

        from video_action_agent.saved_reference import SUBTITLE_REFERENCE

        app = create_ui_app(runtime, {session_token: owner}, lambda: worker_state,
                            source_modes=source_modes, processing_status=observations.read,
                            source_preview=source_preview, source_owner=owner,
                            source_capture=source_capture, notes_service=notes_service,
                            notes_checks=notes_checks,
                            saved_reference=SUBTITLE_REFERENCE if args.fixed_subtitle_reference else None)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                             access_log=False, log_level="warning",
                                             proxy_headers=False))
        address = f"http://127.0.0.1:{args.port}/app"

        def activate_when_ready():
            while not stop.wait(0.1):
                if server.started:
                    if worker_thread is not None:
                        worker_thread.start()
                    if not args.no_open:
                        # A local session token, never a provider key or saved API credential.
                        webbrowser.open(address + "#token=" + session_token)
                    return

        threading.Thread(target=activate_when_ready, daemon=True).start()
        print(json.dumps({"interface": address, "execution_enabled": bool(source_modes),
                          "source_modes": source_modes,
                          "source_capture_enabled": source_capture is not None,
                          "notes_enabled": notes_service is not None,
                          "notes_checks_enabled": notes_checks is not None,
                          "source_preview_available": source_preview is not None},
                         ensure_ascii=False), flush=True)
        server.run()
        return 0
    except (UIProcessingError, SourcePreviewError) as exc:
        print(json.dumps({"error_code": exc.code}), flush=True)
        return 1
    except Exception:  # noqa: BLE001 -- do not echo credentials embedded in database exceptions
        print(json.dumps({"error_code": "local_interface_unavailable"}), flush=True)
        return 1
    finally:
        stop.set()
        if notes_checks is not None:
            notes_checks.close()
        if 'app' in locals() and app.state.video_flows is not None:
            app.state.video_flows.close()
        if source_capture is not None:
            source_capture.close()
        if notes_service is not None:
            notes_service.close()
        if worker_thread is not None and worker_thread.ident is not None:
            worker_thread.join(timeout=2)
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
