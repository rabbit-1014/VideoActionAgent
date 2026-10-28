"""Run one explicitly scoped local product task through its available stages.

This is an operator-invoked runner, not the default background Worker. The CLI
prints only task/stage/status identities; source bytes and API keys stay local.
"""

import argparse
import json
import os
import shutil
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine

from video_action_agent.browser_session import LocalOpenCliSessionProbe
from video_action_agent.deepseek_provider import DeepSeekProvider
from video_action_agent.product_pipeline import ProductPipeline
from video_action_agent.runtime import Runtime
from video_action_agent.source_access import LocalOpenCli
from video_action_agent.zhipu_provider import ZhipuProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-browser", action="store_true", required=True)
    parser.add_argument("--opencli-entry", type=Path, required=True)
    parser.add_argument("--task-id", type=UUID)
    parser.add_argument("--message")
    parser.add_argument("--source")
    parser.add_argument("--key")
    parser.add_argument("--navigation-workaround", action="store_true")
    parser.add_argument("--provider", choices=("deepseek", "zhipu"), default="zhipu")
    args = parser.parse_args()
    creating = args.message is not None or args.source is not None or args.key is not None
    if args.task_id is None and not all((args.message, args.source, args.key)):
        parser.error("Supply --task-id, or --message --source --key to create a scoped task")
    if args.task_id is not None and creating:
        parser.error("--task-id cannot be combined with task creation arguments")
    node = shutil.which("node")
    if node is None:
        parser.error("Node.js is required for the local OpenCLI adapter")
    owner = os.environ["VIDEO_OWNER_ID"]
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"], pool_pre_ping=True)
    try:
        runtime = Runtime(engine, lease_seconds=300)
        if args.task_id is None:
            task = runtime.submit(
                owner, args.key,
                {"message": args.message, "source": args.source,
                 "output_kind": "method_list", "local_browser_actions": ["subtitle"]},
            )
            task_id = task["task_id"]
        else:
            task_id = args.task_id
            runtime.get(owner, task_id)
        adapter = LocalOpenCli(
            node, args.opencli_entry, navigation_workaround=args.navigation_workaround
        )
        probe = LocalOpenCliSessionProbe(node, args.opencli_entry)
        if args.provider == "deepseek":
            provider = DeepSeekProvider()
        else:
            provider = ZhipuProvider(os.environ.get("VIDEO_ZHIPU_MODEL_ID", "glm-5.3"))
        pipeline = ProductPipeline(runtime, owner, adapter, probe, provider)
        result = pipeline.run_until_idle(task_id)
        print(json.dumps({"task_id": str(task_id), "result": result}, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
