"""Test-only worker termination probe. Not registered in the product."""

import os

from sqlalchemy import create_engine

from video_action_agent.runtime import Runtime

engine = create_engine(
    os.environ["VIDEO_TEST_DATABASE_URL"],
    connect_args={"options": "-csearch_path=" + os.environ["PROBE_SCHEMA"]},
)
claim = Runtime(engine).claim("crash-probe", {"form_goal"})
assert claim is not None
os._exit(23)
