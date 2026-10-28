"""Explicitly import a validated saved source; never call capture or claim a new read."""
import shutil
from pathlib import Path

from bridge_contract import atomic, digest
from video_action_agent.source_candidate_bundle import read_bundle, save_bundle

SOURCE_ID = "8db58bff-b71b-5821-b129-b273bb2d7a68"
NOTE_ID = "af565dfa-8c3b-5684-ab6c-5dd73fce936e"
PREVIOUS = Path("D:/CodexWork/VideoActionAgent-visual-intake-20261015")


def seed(sources):
    original = PREVIOUS / "runtime/sources" / SOURCE_ID
    target = sources.output_root / SOURCE_ID
    bundle = read_bundle(original)
    if target.exists():
        if digest(read_bundle(target)) != digest(bundle):
            raise ValueError("Imported source changed")
    else:
        save_bundle(bundle, target)
        for name in ("current-audio-reuse-proof.json", "SOURCE-PROVENANCE.md"):
            shutil.copyfile(original / name, target / name)
        atomic(target / "import-provenance.json", {"kind": "explicit_saved_source_import_not_new_acquisition",
               "origin": str(original), "source_sha256": digest(bundle), "new_source_calls": 0, "new_asr_calls": 0})
    if sources._store.load(SOURCE_ID) is None:
        proof = bundle["source_envelope"]["source_evidence"]
        sources._store.save(SOURCE_ID, {"job_id": SOURCE_ID, "state": "available", "stage": None,
             "bvid": proof["bvid"], "page": proof["page"], "error_code": None},
             {"bvid": proof["bvid"], "page": proof["page"], "allow_local_audio_read": True})
