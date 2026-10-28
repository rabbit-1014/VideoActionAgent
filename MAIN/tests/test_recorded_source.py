import hashlib
import importlib
import json
import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(os.environ.get("VIDEO_RESEARCH_ROOT", Path(__file__).resolve().parents[2]))
EVIDENCE = "26_real_bvid_blackbox/source_evidence.json"
PIN = "481479EE067D0308DDADAB829F02E4AC67DDDCDBA08A94DE710795A402B65547"


def api():
    spec = importlib.util.find_spec("video_action_agent.recorded_source")
    assert spec is not None, "Recorded source ingestion is not implemented"
    return importlib.import_module("video_action_agent.recorded_source")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    # Copy only input materials, never deps, annotations or generated answers.
    target = tmp_path / "26_real_bvid_blackbox"
    target.mkdir()
    shutil.copy2(ROOT / EVIDENCE, target / "source_evidence.json")
    source = ROOT / "26_real_bvid_blackbox/source"
    (target / "source").mkdir()
    for name in (
        "BV1gD421u75J_000000_000129.wav",
        "transcript_faster_whisper_small.json",
        "target_frame_manifest.json",
    ):
        shutil.copy2(source / name, target / "source" / name)
    shutil.copytree(source / "frames", target / "source/frames")
    return tmp_path


def rewrite(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def change_json(root, relative, change, hash_key=None):
    path = root / relative
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    change(data)
    rewrite(path, data)
    if hash_key:
        evidence = json.loads((root / EVIDENCE).read_text(encoding="utf-8-sig"))
        section, field = hash_key
        evidence["extraction"][section][field] = digest(path)
        rewrite(root / EVIDENCE, evidence)
    return digest(root / EVIDENCE)


def test_real_source_is_loaded_without_promoting_old_answers():
    result = api().load_recorded_source(ROOT, EVIDENCE, PIN)
    assert result.mode == "recorded_fixture"
    assert result.bvid == "BV1gD421u75J"
    assert len(result.segments) == 22
    assert len(result.frames) == 7
    assert result.segments[7].text.endswith("这个功能需要联网。")
    assert result.frames[0].timestamp_seconds == 18
    assert result.audio.sha256 == hashlib.sha256(result.audio.content).hexdigest()
    assert result.manifest_generated_at.startswith("2026-08-14")
    assert result.frames[0].material.integrity_basis == "computed_at_import"
    assert not hasattr(result, "video_claims")
    assert not hasattr(result.frames[0], "observation")
    assert not hasattr(result, "current_external_sources")


def test_raw_evidence_manifest_does_not_cross_exclusion_boundary():
    result = api().load_recorded_source(ROOT, EVIDENCE, PIN)
    assert not hasattr(result, "evidence_manifest")
    assert result.evidence_manifest_sha256 == PIN.lower()


def test_missing_historical_frame_digest_is_not_faked(bundle):
    path = bundle / "26_real_bvid_blackbox/source/frames/BV1gD421u75J_018s.jpg"
    path.write_bytes(b"x" * path.stat().st_size)
    result = api().load_recorded_source(bundle, EVIDENCE, PIN)
    assert result.frames[0].material.integrity_basis == "computed_at_import"
    assert any("no recorded digest" in limitation for limitation in result.limitations)


def test_evidence_edit_cannot_reuse_old_pin(bundle):
    with (bundle / EVIDENCE).open("ab") as out:
        out.write(b" ")
    with pytest.raises(api().SourceInputError, match="integrity_mismatch"):
        api().load_recorded_source(bundle, EVIDENCE, PIN)


@pytest.mark.parametrize(
    "name", ["transcript_faster_whisper_small.json", "BV1gD421u75J_000000_000129.wav"]
)
def test_modified_pinned_material_is_rejected(bundle, name):
    with (bundle / "26_real_bvid_blackbox/source" / name).open("ab") as out:
        out.write(b" ")
    with pytest.raises(api().SourceInputError, match="integrity_mismatch"):
        api().load_recorded_source(bundle, EVIDENCE, PIN)


def test_missing_frame_is_explicit_error(bundle):
    (bundle / "26_real_bvid_blackbox/source/frames/BV1gD421u75J_018s.jpg").unlink()
    with pytest.raises(api().SourceInputError, match="material_missing"):
        api().load_recorded_source(bundle, EVIDENCE, PIN)


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["source"].update(bvid="BV_OTHER_VIDEO"),
        lambda d: d["segments"][0].update(end=200),
        lambda d: d["segments"][0].update(start=float("nan")),
        lambda d: d.update(segments=[]),
        lambda d: d["segments"][0].update(text=" "),
    ],
)
def test_invalid_transcript_cannot_become_content(bundle, change):
    pin = change_json(
        bundle,
        "26_real_bvid_blackbox/source/transcript_faster_whisper_small.json",
        change,
        ("local_asr", "sha256"),
    )
    with pytest.raises(api().SourceInputError, match="invalid_transcript"):
        api().load_recorded_source(bundle, EVIDENCE, pin)


def test_frame_from_another_directory_is_not_silently_rebased(bundle):
    pin = change_json(
        bundle,
        "26_real_bvid_blackbox/source/target_frame_manifest.json",
        lambda d: d["frames"][0].update(path="C:/private/secret.jpg"),
        ("target_frames", "manifest_sha256"),
    )
    with pytest.raises(api().SourceInputError, match="path_outside_bundle"):
        api().load_recorded_source(bundle, EVIDENCE, pin)


def test_relative_path_escape_is_rejected(bundle):
    pin = change_json(
        bundle, EVIDENCE, lambda d: d["source"]["audio"].update(path="../outside.wav")
    )
    with pytest.raises(api().SourceInputError, match="path_outside_bundle"):
        api().load_recorded_source(bundle, EVIDENCE, pin)


def test_returned_bytes_are_a_snapshot_not_a_mutable_file_reference(bundle):
    result = api().load_recorded_source(bundle, EVIDENCE, PIN)
    frame = result.frames[0].material
    original = frame.content
    (bundle / "26_real_bvid_blackbox/source/frames/BV1gD421u75J_018s.jpg").write_bytes(b"changed")
    assert frame.content == original
    assert frame.sha256 == hashlib.sha256(original).hexdigest()
