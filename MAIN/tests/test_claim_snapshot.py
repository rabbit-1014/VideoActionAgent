from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

from sqlalchemy import create_engine, event, text
from test_runtime import submit

from video_action_agent.runtime import Runtime


def test_stale_join_snapshot_cannot_issue_second_execution(runtime, engine):
    submit(runtime)
    gate_key = uuid4().int % 2_000_000_000 + 1
    with engine.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar_one()
    delayed_engine = create_engine(engine.url, connect_args={"options": f"-csearch_path={schema}"})

    def inject_gate(conn, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE OF t SKIP LOCKED" in statement:
            statement = (
                f"WITH claim_gate AS MATERIALIZED "
                f"(SELECT pg_advisory_xact_lock({gate_key})) "
                + statement.replace(
                    "FROM tasks t JOIN work_items w",
                    "FROM tasks t CROSS JOIN claim_gate JOIN work_items w",
                )
            )
        return statement, parameters

    event.listen(delayed_engine, "before_cursor_execute", inject_gate, retval=True)
    blocker = engine.connect()
    transaction = blocker.begin()
    blocker.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": gate_key})
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            delayed = pool.submit(Runtime(delayed_engine).claim, "B", {"form_goal"})
            try:
                deadline = monotonic() + 10
                while True:
                    with engine.connect() as observer:
                        waiting = observer.execute(
                            text(
                                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                                "AND objid=:key AND NOT granted"
                            ),
                            {"key": gate_key},
                        ).scalar_one()
                    if waiting:
                        break
                    assert not delayed.done(), "Delayed SELECT did not reach the gate"
                    assert monotonic() < deadline, "No advisory lock wait observed"
                    sleep(0.01)
                first = runtime.claim("A", {"form_goal"})
                assert first is not None
            finally:
                transaction.rollback()
            second = delayed.result(timeout=10)
            assert second is None, f"Duplicate execution permits: A={first.token}, B={second.token}"
            with engine.connect() as observer:
                assert (
                    observer.execute(text("SELECT lease_token FROM work_items")).scalar_one()
                    == first.token
                )
    finally:
        if transaction.is_active:
            transaction.rollback()
        blocker.close()
        delayed_engine.dispose()
