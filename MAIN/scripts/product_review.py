"""Review one saved method-list candidate with a different local model provider.

The result is advisory and never accepts the artifact or completes the task.
"""

import argparse
import json
import os
from uuid import UUID

from sqlalchemy import create_engine

from video_action_agent.artifact_quality import ArtifactReviewError, review_method_list
from video_action_agent.deepseek_provider import DeepSeekProvider
from video_action_agent.runtime import Conflict, Runtime
from video_action_agent.zhipu_provider import ZhipuProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", type=UUID, required=True)
    parser.add_argument("--provider", choices=("deepseek", "zhipu"))
    args = parser.parse_args()
    owner = os.environ["VIDEO_OWNER_ID"]
    engine = create_engine(os.environ["VIDEO_DATABASE_URL"], pool_pre_ping=True)
    try:
        runtime = Runtime(engine)
        artifact = runtime.get_artifact(owner, args.task_id)
        material = next(
            (item for item in runtime.read_formal_materials(owner, args.task_id)
             if str(item["material_id"]) == artifact["material_id"]),
            None,
        )
        if material is None:
            raise Conflict("No current formal material for artifact review")
        draft = artifact["draft"]
        selected = args.provider or (
            "zhipu" if draft["model_id"] == DeepSeekProvider.model_id else "deepseek"
        )
        provider = (
            DeepSeekProvider() if selected == "deepseek"
            else ZhipuProvider(os.environ.get("VIDEO_ZHIPU_MODEL_ID", "glm-5.3"))
        )
        try:
            review = review_method_list(
                draft, material["segments"], draft["user_request"],
                draft["source_ref"], material["payload_hash"], provider,
            )
        except ArtifactReviewError as exc:
            print(json.dumps({"task_id": str(args.task_id), "state": "review_unavailable",
                              "code": exc.code, "provider_code": exc.provider_code}))
            return
        saved = runtime.save_artifact_review(
            owner, args.task_id, artifact["artifact_id"], artifact["revision"], review
        )
        print(json.dumps({"task_id": str(args.task_id), "review_id": saved["review_id"],
                          "recommendation": review["recommendation"],
                          "advisory_only": True, "idempotent": saved["idempotent"]}))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
