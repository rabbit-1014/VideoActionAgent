"""Run the explicit local method-list worker for this configured task owner."""

import argparse
import json
import os
import shutil
import time
from pathlib import Path

from sqlalchemy import create_engine

from video_action_agent.browser_session import LocalOpenCliSessionProbe
from video_action_agent.deepseek_provider import DeepSeekProvider
from video_action_agent.product_pipeline import ProductPipeline
from video_action_agent.product_worker import ProductTaskWorker
from video_action_agent.runtime import Runtime
from video_action_agent.source_access import LocalOpenCli
from video_action_agent.zhipu_provider import ZhipuProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-local-browser", action="store_true", required=True)
    parser.add_argument("--opencli-entry", type=Path, required=True)
    parser.add_argument("--navigation-workaround", action="store_true")
    parser.add_argument("--provider", choices=("deepseek", "zhipu"), default="zhipu")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    node = shutil.which("node")
    if node is None:
        parser.error("Node.js is required for the local OpenCLI adapter")
    owner = os.environ["VIDEO_OWNER_ID"]
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"], pool_pre_ping=True)
    try:
        runtime = Runtime(engine, lease_seconds=300)
        adapter = LocalOpenCli(
            node, args.opencli_entry, navigation_workaround=args.navigation_workaround
        )
        probe = LocalOpenCliSessionProbe(node, args.opencli_entry)
        provider = (
            DeepSeekProvider() if args.provider == "deepseek"
            else ZhipuProvider(os.environ.get("VIDEO_ZHIPU_MODEL_ID", "glm-5.3"))
        )
        pipeline = ProductPipeline(runtime, owner, adapter, probe, provider)
        worker = ProductTaskWorker(runtime, pipeline, owner)
        while True:
            results = worker.run_cycle()
            for result in results:
                print(json.dumps(result, ensure_ascii=False), flush=True)
            if args.once:
                return
            time.sleep(5)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
