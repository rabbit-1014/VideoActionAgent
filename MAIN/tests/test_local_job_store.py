"""DOWN-010 key counterexamples: journal save/restart/order/enumeration and fail-closed refusals."""

import json
import os
import sys
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from video_action_agent import local_job_store
from video_action_agent.local_job_store import JobStoreError, LocalJobStore, job_directories


def _view(job_id, state="queued"):
    return {"job_id": job_id, "state": state, "stage": None, "bvid": "BV1abc",
            "page": 1, "error_code": None}


def _record(job_id, saved_at="2026-01-01T00:00:00+00:00", **changes):
    record = {"schema_version": 1, "owner": "alice", "job_id": job_id, "view": _view(job_id),
              "request": {}, "saved_at": saved_at}
    record.update(changes)
    return record


def _bytes(record):
    return (json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _symlink_dir_probe(tmp_path):
    source, link = tmp_path / "probe-src", tmp_path / "probe-link"
    source.mkdir()
    try:
        link.symlink_to(source, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    return True


# --- Category 1: atomic save, restart read, ordering and bounded enumeration. ---


def test_save_load_restart_roundtrip(tmp_path):
    store = LocalJobStore(tmp_path, "alice")
    job_id = str(uuid4())
    request = {"bvid": "BV1abc", "page": 1, "allow_local_audio_read": True}
    record = store.save(job_id, _view(job_id), request)
    assert set(record) == {"schema_version", "owner", "job_id", "view", "request", "saved_at"}
    assert (record["schema_version"], record["owner"], record["job_id"]) == (1, "alice", job_id)
    assert record["view"] == _view(job_id) and record["request"] == request
    assert datetime.fromisoformat(record["saved_at"]).tzinfo is not None
    assert [file.name for file in (tmp_path / ".jobs").iterdir()] == [f"{job_id}.json"]

    restarted = LocalJobStore(tmp_path, "alice")
    assert restarted.load(job_id) == record
    record["view"]["state"] = "available"          # the returned record is a detached copy
    assert restarted.load(job_id)["view"]["state"] == "queued"

    target = tmp_path / ".jobs" / f"{job_id}.json"
    before = target.read_bytes()
    restarted.load(job_id)
    restarted.records()                            # reads never rewrite the journal
    assert target.read_bytes() == before

    restarted.save(job_id, _view(job_id, state="available"), request)
    assert restarted.load(job_id)["view"]["state"] == "available"
    assert len(list((tmp_path / ".jobs").iterdir())) == 1


def test_save_normalizes_uuid_spelling(tmp_path):
    store = LocalJobStore(tmp_path, "alice")
    job_id = uuid4()
    store.save(f"{{{str(job_id).upper()}}}", _view(str(job_id)), {"page": 2})
    assert store.load(str(job_id))["request"] == {"page": 2}
    assert [path.name for path in (tmp_path / ".jobs").iterdir()] == [f"{job_id}.json"]


def test_records_order_ties_and_limit(tmp_path, monkeypatch):
    store = LocalJobStore(tmp_path, "alice")
    older = [str(uuid4()) for _ in range(3)]
    stamps = ["2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00.000001+00:00",
              "2026-01-01T00:00:01+00:00"]
    for job_id, stamp in zip(older, stamps):
        monkeypatch.setattr(local_job_store, "_now", lambda stamp=stamp: stamp)
        store.save(job_id, _view(job_id), {})
    monkeypatch.setattr(local_job_store, "_now", lambda: "2026-01-01T00:00:02+00:00")
    tied = sorted(str(uuid4()) for _ in range(2))
    for job_id in reversed(tied):                  # same instant, saved out of id order
        store.save(job_id, _view(job_id), {})
    newest_first = [*tied, older[2], older[1], older[0]]
    assert [row["job_id"] for row in store.records()] == newest_first
    assert [row["job_id"] for row in store.records(limit=2)] == newest_first[:2]
    assert store.records(limit=0) == []


def test_records_cap_and_read_only_enumeration(tmp_path, monkeypatch):
    store = LocalJobStore(tmp_path, "alice")
    monkeypatch.setattr(local_job_store, "_now", lambda: "2026-01-01T00:00:00+00:00")
    for _ in range(501):
        job_id = str(uuid4())
        store.save(job_id, _view(job_id), {})
    assert len(store.records()) == 500
    assert len(store.records(limit=1000)) == 500

    empty = tmp_path / "empty"
    assert LocalJobStore(empty, "alice").records() == []
    assert not (empty / ".jobs").exists()          # enumeration never creates anything


def test_job_directories_listing_and_limit(tmp_path):
    kept = sorted(str(uuid4()) for _ in range(3))
    for name in kept:
        (tmp_path / name).mkdir()
    (tmp_path / kept[0] / "inner").mkdir()         # only one level is listed
    (tmp_path / ".jobs").mkdir()
    (tmp_path / ".notes-staging").mkdir()          # temp-style and non-UUID names are ignored
    (tmp_path / "not-a-uuid").mkdir()
    (tmp_path / str(uuid4()).upper()).mkdir()      # non-canonical UUID spelling
    (tmp_path / str(uuid4())).write_text("file")   # a UUID-named file is not a directory
    missing = tmp_path / "missing"
    assert [path.name for path in job_directories(tmp_path)] == kept
    assert [path.name for path in job_directories(tmp_path, limit=2)] == kept[:2]
    assert job_directories(missing) == [] and not missing.exists()


def test_job_directories_cap(tmp_path):
    for _ in range(502):
        (tmp_path / str(uuid4())).mkdir()
    assert len(job_directories(tmp_path)) == 500
    assert len(job_directories(tmp_path, limit=1000)) == 500


def test_records_ignore_non_canonical_names(tmp_path):
    store = LocalJobStore(tmp_path, "alice")
    jobs = tmp_path / ".jobs"
    jobs.mkdir()
    (jobs / "not-a-uuid.json").write_text("{}")
    (jobs / f"{str(uuid4()).upper()}.json").write_text("{}")
    (jobs / f"{uuid4()}.json").mkdir()             # a directory named like a record
    assert store.records() == []


# --- Category 2: foreign owner, wrong IDs, corruption, NaN and link escapes. ---


def test_foreign_owner_cannot_read_or_overwrite(tmp_path):
    alice, bob = LocalJobStore(tmp_path, "alice"), LocalJobStore(tmp_path, "bob")
    job_id = str(uuid4())
    alice.save(job_id, _view(job_id), {"page": 1})
    target = tmp_path / ".jobs" / f"{job_id}.json"
    before = target.read_bytes()
    assert bob.load(job_id) is None
    assert bob.records() == []
    with pytest.raises(JobStoreError) as exc:
        bob.save(job_id, _view(job_id, state="available"), {"page": 1})
    assert exc.value.code == "job_storage_unavailable"
    assert target.read_bytes() == before
    assert alice.load(job_id)["owner"] == "alice"
    other = str(uuid4())
    bob.save(other, _view(other), {})
    assert alice.load(other) is None
    assert [row["job_id"] for row in alice.records()] == [job_id]


def test_invalid_input_refused_without_writing(tmp_path):
    good = str(uuid4())
    cases = [
        ("save", (good, {"job_id": str(uuid4())}, {})),                       # view.job_id mismatch
        ("save", (good, ["not", "a", "dict"], {})),
        ("save", (good, {"job_id": good, "nested": {"x": {1: "int key"}}}, {})),
        ("save", (good, _view(good) | {"bad": float("nan")}, {})),
        ("save", (good, _view(good) | {"bad": float("inf")}, {})),
        ("save", (good, _view(good), {"set": {1, 2}})),
        ("save", (good, _view(good), {"tuple": (1, 2)})),
        ("save", (good, _view(good), {"nested": [{"tuple": (1, 2)}]})),
        ("save", (good, _view(good), {"nested": [float("nan")]})),
        ("save", (good, _view(good), {"when": datetime.now(tz=UTC)})),
        ("save", (good, _view(good), {"bad": float("nan")})),
        ("save", (good, _view(good) | {"pad": "x" * (1024 * 1024 + 64)}, {})),  # over 1MiB
        ("save", ("../../evil", _view(good), {})),
        ("save", ("not-a-uuid", _view(good), {})),
        ("save", (None, _view(good), {})),
        ("load", ("../../evil",)),
        ("load", ("not-a-uuid",)),
        ("load", (42,)),
        ("records", (-1,)),
        ("records", ("5",)),
    ]
    for name, args in cases:
        store = LocalJobStore(tmp_path, "alice")
        with pytest.raises(JobStoreError) as exc:
            getattr(store, name)(*args)
        assert exc.value.code == "job_storage_unavailable", (name, args)
        assert not (tmp_path / ".jobs").exists(), name   # nothing created, nothing written


def test_corrupt_and_foreign_files_quiet_and_protected(tmp_path):
    store = LocalJobStore(tmp_path, "alice")
    (tmp_path / ".jobs").mkdir()
    mutated = []
    job_id = str(uuid4())
    missing = _record(job_id)
    del missing["saved_at"]
    mutated.append((job_id, _bytes(missing)))
    job_id = str(uuid4())
    wrong_id = _record(job_id)
    wrong_id["job_id"] = str(uuid4())                # record ID ≠ canonical file name
    mutated.append((job_id, _bytes(wrong_id)))
    for changes in (
        {"unknown": 1}, {"schema_version": 2}, {"schema_version": True}, {"owner": "bob"},
        {"view": {"job_id": str(uuid4()), "state": "queued"}},       # view.job_id mismatch
        {"saved_at": "2026-01-01T00:00:00"}, {"saved_at": "2026-01-01T00:00:00+01:00"},
        {"saved_at": "yesterday"},
    ):
        job_id = str(uuid4())
        mutated.append((job_id, _bytes(_record(job_id, **changes))))
    job_id = str(uuid4())
    nan = _bytes(_record(job_id, view={"job_id": job_id, "bad": "__nan__"}))
    mutated.append((job_id, nan.replace(b'"__nan__"', b"NaN")))
    mutated.extend([(str(uuid4()), b"{broken"), (str(uuid4()), b"[]"),
                     (str(uuid4()), "béta".encode("utf-16"))])
    for job_id, payload in mutated:
        target = tmp_path / ".jobs" / f"{job_id}.json"
        target.write_bytes(payload)
        assert store.load(job_id) is None, job_id
        assert all(row["job_id"] != job_id for row in store.records())
        before = target.read_bytes()
        with pytest.raises(JobStoreError):
            store.save(job_id, _view(job_id), {})
        assert target.read_bytes() == before, job_id    # bad files are never overwritten
    fresh = str(uuid4())
    store.save(fresh, _view(fresh), {})
    assert store.load(fresh)["job_id"] == fresh         # storage stays usable afterwards


def test_jobs_path_conflict_fails_stable(tmp_path):
    (tmp_path / ".jobs").write_text("not a directory")
    store = LocalJobStore(tmp_path, "alice")
    job_id = str(uuid4())
    with pytest.raises(JobStoreError) as exc:
        store.save(job_id, _view(job_id), {})
    assert exc.value.code == "job_storage_unavailable"
    assert store.load(job_id) is None
    assert store.records() == []


def test_symlinked_record_file_never_read_or_overwritten(tmp_path):
    if not _symlink_dir_probe(tmp_path):
        pytest.skip("symlink creation unavailable")
    outside = tmp_path / "outside"
    secret = outside / "secret.json"
    secret.write_text("secret")
    root = tmp_path / "root"
    (root / ".jobs").mkdir(parents=True)
    job_id = str(uuid4())
    os.symlink(secret, root / ".jobs" / f"{job_id}.json")
    store = LocalJobStore(root, "alice")
    assert store.load(job_id) is None                    # never reads through the link
    with pytest.raises(JobStoreError):
        store.save(job_id, _view(job_id), {})
    assert secret.read_text() == "secret"                # the external file is untouched
    assert [path.name for path in job_directories(root)] == []


def test_symlinked_jobs_dir_fails_closed(tmp_path):
    if not _symlink_dir_probe(tmp_path):
        pytest.skip("symlink creation unavailable")
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    os.symlink(outside, root / ".jobs", target_is_directory=True)
    store = LocalJobStore(root, "alice")
    job_id = str(uuid4())
    with pytest.raises(JobStoreError):
        store.save(job_id, _view(job_id), {})
    with pytest.raises(JobStoreError):
        store.load(job_id)                               # fail closed, not a quiet None
    with pytest.raises(JobStoreError):
        store.records()
    with pytest.raises(JobStoreError):
        job_directories(root)
    assert list(outside.iterdir()) == []                 # nothing escaped to the link target


def test_job_directories_symlink_escape_fails_closed(tmp_path):
    if not _symlink_dir_probe(tmp_path):
        pytest.skip("symlink creation unavailable")
    outside = tmp_path / "outside"
    outside.mkdir()
    kept = tmp_path / str(uuid4())
    kept.mkdir()
    escape = tmp_path / str(uuid4())
    os.symlink(outside, escape, target_is_directory=True)
    with pytest.raises(JobStoreError):
        job_directories(tmp_path)
    assert list(outside.iterdir()) == []


def test_job_directories_ignores_internal_symlink(tmp_path):
    if not _symlink_dir_probe(tmp_path):
        pytest.skip("symlink creation unavailable")
    real = tmp_path / "real-target"
    real.mkdir()
    kept = tmp_path / str(uuid4())
    kept.mkdir()
    linked = tmp_path / str(uuid4())
    os.symlink(real, linked, target_is_directory=True)   # linked within root: ignored
    assert [path.name for path in job_directories(tmp_path)] == [kept.name]


def test_junction_jobs_dir_fails_closed(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows junctions")
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    _winapi.CreateJunction(str(outside), str(root / ".jobs"))
    store = LocalJobStore(root, "alice")
    job_id = str(uuid4())
    with pytest.raises(JobStoreError):
        store.save(job_id, _view(job_id), {})
    with pytest.raises(JobStoreError):
        store.load(job_id)
    with pytest.raises(JobStoreError):
        store.records()
    with pytest.raises(JobStoreError):
        job_directories(root)
    assert list(outside.iterdir()) == []                 # nothing escaped to the junction


def test_job_directories_junction_internal_ignored_and_escape_fails(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows junctions")
    import _winapi

    real = tmp_path / "real-target"
    real.mkdir()
    kept = tmp_path / str(uuid4())
    kept.mkdir()
    internal = tmp_path / str(uuid4())
    _winapi.CreateJunction(str(real), str(internal))     # within root: ignored
    assert [path.name for path in job_directories(tmp_path)] == [kept.name]

    outside = tmp_path / "outside"
    outside.mkdir()
    escape_root = tmp_path / "escape-root"
    escape_root.mkdir()
    (escape_root / str(uuid4())).mkdir()
    escape = escape_root / str(uuid4())
    _winapi.CreateJunction(str(outside), str(escape))    # outside root: fail closed
    with pytest.raises(JobStoreError):
        job_directories(escape_root)
    assert list(outside.iterdir()) == []
