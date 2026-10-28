"""Operator-invoked, independently authorized local audio method-list task.

Only exact public audio is fetched; local ASR stays offline. Text generation may
use the selected model. This entry never accepts a candidate for the owner.
"""

import argparse
import json
import os
from uuid import UUID

from sqlalchemy import create_engine, text

from video_action_agent.audio_session import AudioSession
from video_action_agent.deepseek_provider import DeepSeekProvider
from video_action_agent.product_pipeline import ProductPipeline
from video_action_agent.runtime import Runtime
from video_action_agent.zhipu_provider import ZhipuProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-audio-read", action="store_true", required=True)
    parser.add_argument("--task-id", type=UUID)
    parser.add_argument("--message")
    parser.add_argument("--source")
    parser.add_argument("--key")
    parser.add_argument("--provider", choices=("deepseek", "zhipu"), default="zhipu")
    args = parser.parse_args()
    creating = any((args.message, args.source, args.key))
    if args.task_id is None and not all((args.message, args.source, args.key)):
        parser.error("Supply --task-id or --message --source --key for an explicit method-list request")
    if args.task_id is not None and creating:
        parser.error("Named tasks cannot be combined with task creation arguments")
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"], pool_pre_ping=True)
    try:
        owner = os.environ["VIDEO_OWNER_ID"]
        runtime = Runtime(engine, lease_seconds=60)
        if args.task_id is None:
            task = runtime.submit(owner, args.key, {
                "message": args.message, "source": args.source, "output_kind": "method_list",
                "source_mode": "audio_transcription", "local_source_actions": ["audio_transcription"],
            })
            task_id = task["task_id"]
        else:
            task_id = args.task_id
            runtime.get(owner, task_id)
            with engine.connect() as conn:
                request = conn.execute(text("""
                    SELECT c.input FROM tasks t JOIN task_contracts c
                      ON c.task_id=t.task_id AND c.version=t.current_version
                    WHERE t.task_id=:task AND t.owner_id=:owner
                """), {"task": task_id, "owner": owner}).scalar_one()
            if request.get("source_mode") != "audio_transcription":
                parser.error("This task was not explicitly authorized for audio transcription")
        provider = (DeepSeekProvider() if args.provider == "deepseek" else
                    ZhipuProvider(os.environ.get("VIDEO_ZHIPU_MODEL_ID", "glm-5.3")))
        pipeline = ProductPipeline(runtime, owner, None, None, provider,
                                   audio_session_factory=AudioSession)
        result = pipeline.run_until_idle(task_id)
        print(json.dumps({"task_id": str(task_id), "result": result}, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
