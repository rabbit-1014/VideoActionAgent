"""Local job journal in <root>/.jobs plus safe one-level directory enumeration.

Standard library only. A record stores the caller's own job view and request so a
service can recover state after restart; a record never proves task completion,
and nothing here replays work or changes job state. All operations are stateless
per call (no in-memory cache or lock), so concurrent instances on the same root
rely on atomic same-volume replaces. Every failure raises ``JobStoreError`` with
the single stable code ``job_storage_unavailable``; messages never contain paths
or record data.
"""

import json
import math
import os
import tempfile
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

MAX_RECORD_BYTES = 1024 * 1024
MAX_RECORDS = 500
RECORD_FIELDS = {"schema_version", "owner", "job_id", "view", "request", "saved_at"}


class JobStoreError(ValueError):
    """One stable storage code; the reason token is fixed and carries no private data."""

    def __init__(self, reason):
        self.code = "job_storage_unavailable"
        self.reason = reason
        super().__init__(f"job_storage_unavailable: {reason}")


def _now():
    return datetime.now(UTC).isoformat()


def _local_root(root):
    try:
        path = Path(root)
        raw = str(path)
    except TypeError:
        raise JobStoreError("invalid_root") from None
    if "://" in raw or raw.startswith(("\\\\", "//")):
        raise JobStoreError("invalid_root")
    if path.is_symlink() or path.is_junction():
        raise JobStoreError("linked_storage")
    try:
        resolved = path.resolve()
    except OSError:
        raise JobStoreError("invalid_root") from None
    if str(resolved).startswith(("\\\\", "//")):
        raise JobStoreError("invalid_root")
    return resolved


def _owner(owner):
    if not isinstance(owner, str) or not owner:
        raise JobStoreError("invalid_owner")
    return owner


def _job_id(value):
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError:
            pass
    raise JobStoreError("invalid_job_id")


def _canonical_name(name):
    try:
        return name if str(UUID(name)) == name else None
    except (ValueError, TypeError, AttributeError):
        return None


def _json_value(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise JobStoreError("invalid_json_value")
            _json_value(item)
    elif isinstance(value, list):
        for item in value:
            _json_value(item)
    elif isinstance(value, (str, bool, int)) or value is None:
        return
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise JobStoreError("invalid_json_value")
    else:
        raise JobStoreError("invalid_json_value")


def _utc_instant(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed


def _reject_constant(name):
    raise ValueError(name)


def _serialize(record):
    return (json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _parse_record(payload, identity):
    """Structurally validate one journal payload; None when it is not a valid record."""
    try:
        record = json.loads(payload.decode("utf-8"), parse_constant=_reject_constant)
        valid = (
            isinstance(record, dict) and set(record) == RECORD_FIELDS
            and type(record["schema_version"]) is int and record["schema_version"] == 1
            and type(record["owner"]) is str
            and type(record["job_id"]) is str and record["job_id"] == identity
            and isinstance(record["view"], dict) and record["view"].get("job_id") == identity
            and isinstance(record["request"], dict)
            and _utc_instant(record["saved_at"]) is not None
        )
    except (ValueError, TypeError, KeyError, RecursionError):
        return None
    return record if valid else None


def _read_file(path, identity):
    """Read one journal file; None when absent or not a valid record; raises on storage errors."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None
        payload = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        raise JobStoreError("storage_read_failed") from None
    return _parse_record(payload, identity)


def _linked(path):
    return path.is_symlink() or path.is_junction()


def _limit(value):
    if type(value) is not int or value < 0:
        raise JobStoreError("invalid_limit")
    return min(value, MAX_RECORDS)


class LocalJobStore:
    """Owner-scoped journal of local job views stored as <root>/.jobs/<uuid>.json."""

    def __init__(self, root, owner):
        self.root = _local_root(root)
        self.owner = _owner(owner)
        self._jobs_dir = self.root / ".jobs"

    def save(self, job_id, view, request):
        """Atomically write one owner record; refuses to overwrite other owners or bad files."""
        identity = _job_id(job_id)
        if not isinstance(view, dict):
            raise JobStoreError("invalid_view")
        if type(view.get("job_id")) is not str or view["job_id"] != identity:
            raise JobStoreError("view_job_id_mismatch")
        if not isinstance(request, dict):
            raise JobStoreError("invalid_request")
        try:
            _json_value(view)
            _json_value(request)
        except RecursionError:
            raise JobStoreError("invalid_json_value") from None
        record = {"schema_version": 1, "owner": self.owner, "job_id": identity,
                  "view": view, "request": request, "saved_at": _now()}
        payload = _serialize(record)
        if len(payload) > MAX_RECORD_BYTES:
            raise JobStoreError("record_size_limit")
        self._write(identity, payload)
        return deepcopy(record)

    def _write(self, identity, payload):
        directory = self._jobs_dir
        if _linked(self.root) or _linked(directory):
            raise JobStoreError("linked_storage")
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise JobStoreError("storage_write_failed") from None
        if _linked(directory):
            raise JobStoreError("linked_storage")
        target = directory / f"{identity}.json"
        if _linked(target):
            raise JobStoreError("linked_record")
        existing = _read_file(target, identity)
        if existing is not None:
            if existing["owner"] != self.owner:
                raise JobStoreError("record_owner_conflict")
        elif target.exists():
            raise JobStoreError("record_corrupt")
        try:
            handle, staged = tempfile.mkstemp(dir=directory, prefix=".job-", suffix=".tmp")
        except OSError:
            raise JobStoreError("storage_write_failed") from None
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, target)
        except OSError:
            try:
                os.unlink(staged)
            except OSError:
                pass
            raise JobStoreError("storage_write_failed") from None

    def load(self, job_id):
        """Return this owner's valid record or None; never raises for absent or foreign records."""
        identity = _job_id(job_id)
        record = self._read(identity)
        if record is None or record["owner"] != self.owner:
            return None
        return record

    def _read(self, identity):
        directory = self._jobs_dir
        if _linked(self.root) or _linked(directory):
            raise JobStoreError("linked_storage")
        if not directory.is_dir():
            return None
        return _read_file(directory / f"{identity}.json", identity)

    def records(self, limit=MAX_RECORDS, *, strict=False):
        """Owned valid records; strict inventory refuses unreadable UUID records."""
        count = _limit(limit)
        directory = self._jobs_dir
        if _linked(self.root) or _linked(directory):
            raise JobStoreError("linked_storage")
        try:
            entries = list(os.scandir(directory)) if directory.is_dir() else []
        except FileNotFoundError:
            entries = []
        except OSError:
            raise JobStoreError("storage_read_failed") from None
        rows = []
        for entry in entries:
            identity = None
            if entry.name.endswith(".json"):
                identity = _canonical_name(entry.name[:-5])
            if identity is None:
                continue
            record = _read_file(directory / entry.name, identity)
            if strict and record is None:
                raise JobStoreError("invalid_record")
            if record is not None and record["owner"] == self.owner:
                rows.append((_utc_instant(record["saved_at"]), record))
        rows.sort(key=lambda row: row[1]["job_id"])
        rows.sort(key=lambda row: row[0], reverse=True)
        return [record for _, record in rows[:count]]


def job_directories(root, limit=MAX_RECORDS):
    """UUID-named job directories one level under root, name-sorted; read-only, link-safe."""
    base = _local_root(root)
    count = _limit(limit)
    if _linked(base / ".jobs"):
        raise JobStoreError("linked_storage")
    try:
        entries = list(os.scandir(base)) if base.is_dir() else []
    except FileNotFoundError:
        entries = []
    except OSError:
        raise JobStoreError("storage_read_failed") from None
    found = []
    for entry in entries:
        if _canonical_name(entry.name) is None:
            continue
        path = base / entry.name
        if _linked(path):
            try:
                contained = path.resolve().is_relative_to(base)
            except OSError:
                raise JobStoreError("storage_read_failed") from None
            if not contained:
                raise JobStoreError("linked_storage")   # a linked job directory escaping root
            continue                                    # links within root are ignored
        if not path.is_dir():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            raise JobStoreError("storage_read_failed") from None
        if not resolved.is_relative_to(base):
            raise JobStoreError("linked_storage")
        found.append(path)
    found.sort(key=lambda item: item.name)
    return found[:count]
