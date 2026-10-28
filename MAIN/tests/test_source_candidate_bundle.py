"""A local candidate package is readable, scoped, and never a formal result."""

import hashlib
import importlib

import pytest
from test_asr_formal_source import BVID, envelope

from video_action_agent.source_access import VideoRef


def api():
    return importlib.import_module("video_action_agent.source_candidate_bundle")


def candidate(tmp_path):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"controlled local audio fixture")
    body = envelope()
    body["transcription"]["audio_sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
    return api().build_bundle(body, VideoRef(BVID, 42), "recorded_audio", audio)


def test_recorded_candidate_roundtrip_preserves_identity_and_limits(tmp_path):
    bundle = candidate(tmp_path)
    target = tmp_path / "bundle"
    saved = api().save_bundle(bundle, target)
    loaded = api().read_bundle(target)
    assert loaded == bundle
    assert saved["acquisition"] == "recorded_audio" and saved["segments"] == 1
    assert loaded["completed"] is False and loaded["formal_task"] is False
    markdown = (target / "transcript.md").read_text(encoding="utf-8")
    assert "历史" in markdown and "ASR" in markdown and "VAD" in markdown
    assert "00:00:01,000 --> 00:00:03,000" in (target / "transcript.srt").read_text(encoding="utf-8")
    assert loaded["source_envelope"]["source_evidence"]["cid"] == 41961066345


def test_scope_or_local_audio_hash_mismatch_rejects_candidate(tmp_path):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"wrong audio")
    with pytest.raises(api().CandidateBundleError, match="source_mismatch"):
        api().build_bundle(envelope(), VideoRef(BVID, 1), "recorded_audio", audio)
    with pytest.raises(api().CandidateBundleError, match="audio_integrity_mismatch"):
        api().build_bundle(envelope(), VideoRef(BVID, 42), "recorded_audio", audio)


def test_existing_output_and_modified_package_are_not_silently_accepted(tmp_path):
    bundle = candidate(tmp_path)
    target = tmp_path / "bundle"
    api().save_bundle(bundle, target)
    original = (target / "transcript.md").read_bytes()
    with pytest.raises(api().CandidateBundleError, match="destination_exists"):
        api().save_bundle(bundle, target)
    assert (target / "transcript.md").read_bytes() == original
    (target / "transcript.md").write_text("changed", encoding="utf-8")
    with pytest.raises(api().CandidateBundleError, match="bundle_integrity_mismatch"):
        api().read_bundle(target)


def test_failed_export_never_publishes_partial_package(tmp_path, monkeypatch):
    bundle = candidate(tmp_path)
    target = tmp_path / "bundle"
    write_text = type(target).write_text

    def fail_srt(path, *args, **kwargs):
        if path.name == "transcript.srt":
            raise OSError("controlled disk failure")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(type(target), "write_text", fail_srt)
    with pytest.raises(api().CandidateBundleError, match="bundle_write_failed"):
        api().save_bundle(bundle, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".source-bundle-*"))
