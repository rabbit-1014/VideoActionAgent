import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def engine():
    url = os.environ["VIDEO_TEST_DATABASE_URL"]
    base = create_engine(url)
    assert base.url.database.endswith("_test"), "Use a dedicated test database"
    schema = "t_" + uuid4().hex
    with base.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    db = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    with db.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    try:
        yield db
    finally:
        db.dispose()
        with base.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        base.dispose()


@pytest.fixture
def runtime(engine):
    from video_action_agent.runtime import Runtime

    return Runtime(engine)
