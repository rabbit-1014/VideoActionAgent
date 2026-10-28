import pytest
from fastapi.testclient import TestClient
from test_source_candidate_bundle import api, candidate


def test_saved_source_preview_is_owner_scoped_and_remains_a_recorded_candidate(tmp_path):
    from video_action_agent.local_ui_app import create_ui_app
    from video_action_agent.source_preview import load_source_preview

    directory = tmp_path / "bundle"
    api().save_bundle(candidate(tmp_path), directory)
    preview = load_source_preview(directory)
    assert preview["acquisition"] == "recorded_audio"
    assert preview["completed"] is False and preview["formal_task"] is False
    assert preview["source_ref"]["page"] == 42 and preview["source_ref"]["cid"] == 41961066345
    assert preview["segments"][0]["start"] == 1
    assert "历史" in preview["markdown"] and "00:00:01,000 --> 00:00:03,000" in preview["srt"]
    client = TestClient(create_ui_app(object(), {"alice-session": "alice", "bob-session": "bob"},
                                     source_modes=(), source_preview=preview, source_owner="alice"),
                        base_url="http://127.0.0.1")
    assert client.get("/app/source-candidate").status_code == 401
    assert client.get("/app/source-candidate", headers={"Authorization": "Bearer bob-session"}).status_code == 404
    client.post("/app/session", headers={"Authorization": "Bearer alice-session"})
    response = client.get("/app/source-candidate?path=unrelated-private-file")
    assert response.status_code == 200 and response.json() == preview
    assert "source_envelope" not in response.text and str(directory) not in response.text
    assert client.get("/app/status").json()["execution_enabled"] is False


def test_source_preview_rejects_a_modified_bundle_without_exposing_paths(tmp_path):
    from video_action_agent.source_preview import SourcePreviewError, load_source_preview

    directory = tmp_path / "bundle"
    api().save_bundle(candidate(tmp_path), directory)
    (directory / "transcript.md").write_text("changed private text", encoding="utf-8")
    with pytest.raises(SourcePreviewError, match="source_bundle_unavailable") as rejected:
        load_source_preview(directory)
    assert str(directory) not in str(rejected.value) and "private" not in str(rejected.value)
