from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient

from video_action_agent.local_ui_app import create_ui_app
from video_action_agent.saved_reference import SUBTITLE_REFERENCE

TARGET = str(SUBTITLE_REFERENCE.candidate_id)
OLD = "be7990b8-025d-471e-8538-bb520f9ae2ad"


class SavedNotes:
    owner = "alice"

    def __init__(self):
        self.value = {"candidate": {"title": "原笔记"}, "markdown": "原稿",
                      "reading_markdown": "原稿", "formal_task": False, "completed": False,
                      "revision": {"version": 7,
                                   "record_fingerprint": SUBTITLE_REFERENCE.record_fingerprint}}

    def candidate(self, identity):
        value = deepcopy(self.value)
        if str(identity) == OLD:
            value["revision"].update(version=5, record_fingerprint="a" * 64)
        return value


def setup(tmp_path):
    text = "# 独立参考\n\n来源文档日期：2022-08-10。\n"
    path = tmp_path / "reference.md"
    path.write_bytes(text.encode("utf-8"))
    binding = replace(SUBTITLE_REFERENCE, path=path, payload_sha256=sha256(path.read_bytes()).hexdigest())
    notes = SavedNotes()
    client = TestClient(create_ui_app(object(), {"a": "alice", "b": "bob"},
                                     source_modes=(), notes_service=notes, saved_reference=binding),
                        base_url="http://127.0.0.1")
    return notes, client, path


def test_exact_saved_version_read_and_original_download_auth_and_old_version(tmp_path):
    notes, client, path = setup(tmp_path)
    endpoint = f"/app/notes/{TARGET}/reference"
    for route in (endpoint, endpoint + "/download"):
        assert client.get(route).status_code == 401
        assert client.get(route, headers={"Authorization": "Bearer b"}).status_code == 404
    headers = {"Authorization": "Bearer a"}
    projected = client.get(f"/app/notes/{TARGET}/candidate", headers=headers).json()
    assert projected["independent_reference"]["state"] == "available"
    assert projected["independent_reference"]["source_document_updated"] == "2022-08-10"
    payload = client.get(endpoint, headers=headers)
    assert payload.status_code == 200 and payload.json()["markdown"] == path.read_text("utf-8")
    download = client.get(endpoint + "/download", headers=headers)
    assert download.status_code == 200 and download.content == path.read_bytes()
    assert "attachment" in download.headers["content-disposition"]
    assert notes.value.get("independent_reference") is None  # No persistent candidate mutation.
    assert client.get(f"/app/notes/{OLD}/candidate", headers=headers).json()["independent_reference"] is None
    assert client.get(f"/app/notes/{OLD}/reference", headers=headers).status_code == 404


def test_version_or_fingerprint_change_is_unavailable_without_wrong_fallback(tmp_path):
    notes, client, _ = setup(tmp_path)
    headers = {"Authorization": "Bearer a"}
    for change in ({"version": 8}, {"record_fingerprint": "b" * 64}):
        notes.value["revision"].update(change)
        value = client.get(f"/app/notes/{TARGET}/candidate", headers=headers).json()
        assert value["independent_reference"]["state"] == "unavailable"
        for suffix in ("reference", "reference/download"):
            reply = client.get(f"/app/notes/{TARGET}/{suffix}", headers=headers)
            assert reply.status_code == 409 and reply.json()["detail"] == "reference_version_unavailable"
        notes.value["revision"].update(version=7,
                                        record_fingerprint=SUBTITLE_REFERENCE.record_fingerprint)


def test_missing_tampered_or_unreadable_reference_is_honestly_unavailable(tmp_path, monkeypatch):
    _, client, path = setup(tmp_path)
    headers = {"Authorization": "Bearer a"}
    original = path.read_bytes()
    for altered in (None, original + b" changed"):
        if altered is None:
            path.unlink()
        else:
            path.write_bytes(altered)
        value = client.get(f"/app/notes/{TARGET}/candidate", headers=headers).json()
        assert value["independent_reference"]["state"] == "unavailable"
        for suffix in ("reference", "reference/download"):
            reply = client.get(f"/app/notes/{TARGET}/{suffix}", headers=headers)
            assert reply.status_code == 503 and reply.json()["detail"] == "reference_unavailable"
    path.write_bytes(original)
    real_open = Path.open

    def denied(selected, *args, **kwargs):
        if selected == path:
            raise PermissionError("private filesystem error")
        return real_open(selected, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    reply = client.get(f"/app/notes/{TARGET}/reference", headers=headers)
    assert reply.status_code == 503 and "private" not in reply.text
