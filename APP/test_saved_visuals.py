"""Offline contract checks for reading an owned, saved visual observation."""
import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VISUAL_ROOT = Path("D:/CodexWork/VideoActionAgent-visual-intake-20261015")
sys.path.insert(0, "C:/Users/泽/Documents/VideoActionAgent/src")
sys.path.insert(0, "D:/CodexWork/VideoActionAgent-downstream-20261014")
sys.path.insert(0, str(VISUAL_ROOT))

from bridge_contract import BridgeError, digest
from native_notes import NativeNotes
from video_action_agent.source_candidate_bundle import read_bundle
from vision_pipeline import VisionPipeline
from vision_policy import VisualIntents

from saved_visuals import SavedVisuals

OWNER = "local-developer"
SOURCE_ID = "8db58bff-b71b-5821-b129-b273bb2d7a68"
DONOR_ID = "af565dfa-8c3b-5684-ab6c-5dd73fce936e"
RUNTIME = VISUAL_ROOT / "runtime"


class BundleReader:
    def __init__(self, owner, bundle):
        self.owner = owner
        self._bundle = bundle

    def bundle(self, _source_id):
        return self._bundle


def offline_donor(runtime=RUNTIME, owner=OWNER):
    bundle = read_bundle(runtime / "sources" / SOURCE_ID)
    source = BundleReader(owner, bundle)
    notes = NativeNotes(owner, runtime / "notes", source, lambda *a, **k: None,
                        lambda *_: "offline")
    intents = VisualIntents(runtime / "visual-scopes", owner)
    pipeline = VisionPipeline(runtime / "vision", intents, lambda *a, **k: None)
    return bundle, notes, pipeline


class SavedVisualsTests(unittest.TestCase):
    def test_real_saved_packet_loads_as_unverified_historical_observation(self):
        bundle, notes, pipeline = offline_donor()
        reader = SavedVisuals(OWNER, lambda source_id: [(DONOR_ID, notes, pipeline)]
                              if source_id == SOURCE_ID else [])
        descriptor = reader.describe(SOURCE_ID, bundle)
        self.assertEqual(set(descriptor), {"owner", "source_id", "donor_id", "source_sha256",
                                           "packet_sha256", "scope_sha256"})
        self.assertEqual(descriptor["donor_id"], DONOR_ID)
        self.assertNotIn("path", json.dumps(descriptor).lower())
        packet = reader.load(descriptor, SOURCE_ID, bundle)
        self.assertEqual(packet["source_sha256"], digest(bundle))
        self.assertEqual(packet["role"], "unverified_current_frame_model_observation")
        self.assertFalse(packet["quality_approved"])
        proof = bundle["source_envelope"]["source_evidence"]
        self.assertEqual(packet["source"], {"bvid": proof["bvid"], "page": proof["page"], "cid": proof["cid"]})

    def test_pinned_lookup_avoids_library_scan_and_rechecks_owner_next_read(self):
        bundle, notes, pipeline = offline_donor()
        descriptor = SavedVisuals(OWNER, lambda _: [(DONOR_ID, notes, pipeline)]).describe(SOURCE_ID, bundle)

        def do_not_scan(_source_id):
            raise AssertionError("Pinned donor must not enumerate the library")

        def lookup(source_id, donor_id):
            self.assertEqual((source_id, donor_id), (SOURCE_ID, DONOR_ID))
            return DONOR_ID, notes, pipeline

        reader = SavedVisuals(OWNER, do_not_scan, lookup=lookup)
        packet = reader.load(descriptor, SOURCE_ID, bundle)
        self.assertEqual(packet["source_sha256"], digest(bundle))
        notes.owner = "another-owner"
        with self.assertRaises(BridgeError):
            reader.load(descriptor, SOURCE_ID, bundle)
        missing = SavedVisuals(OWNER, do_not_scan, lookup=lambda *_: None)
        with self.assertRaises(BridgeError):
            missing.describe(SOURCE_ID, bundle, donor_id=DONOR_ID)

    def test_exact_donor_and_changed_saved_packet_fail_closed(self):
        temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temp.cleanup)
        copied = Path(temp.name)
        shutil.copytree(RUNTIME / "sources", copied / "sources")
        shutil.copytree(RUNTIME / "notes", copied / "notes")
        shutil.copytree(RUNTIME / "vision", copied / "vision")
        shutil.copytree(RUNTIME / "visual-scopes", copied / "visual-scopes")
        packet_path = copied / "vision" / DONOR_ID / "visual-material.json"
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        for frame in packet["frames"]:
            original = Path(frame["path"])
            relative = original.relative_to(RUNTIME / "vision")
            frame["path"] = str(copied / "vision" / relative)
        packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        row_path = copied / "notes" / ".native-jobs" / f"{DONOR_ID}.json"
        row = json.loads(row_path.read_text(encoding="utf-8"))
        row["vision_manifest_sha256"] = hashlib.sha256(packet_path.read_bytes()).hexdigest()
        row_path.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        bundle, notes, pipeline = offline_donor(copied)
        calls = [(DONOR_ID, notes, pipeline), ("another-donor", notes, pipeline)]
        reader = SavedVisuals(OWNER, lambda _source_id: calls)
        descriptor = reader.describe(SOURCE_ID, bundle, donor_id=DONOR_ID)
        self.assertEqual(descriptor["donor_id"], DONOR_ID)
        wrong_owner = dict(descriptor, owner="other-owner")
        wrong_source = dict(descriptor, source_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        with self.assertRaises(BridgeError):
            reader.load(wrong_owner, SOURCE_ID, bundle)
        with self.assertRaises(BridgeError):
            reader.load(wrong_source, SOURCE_ID, bundle)
        with self.assertRaises(BridgeError):
            reader.describe(SOURCE_ID, bundle, donor_id="another-donor")
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["scope_sha256"] = "0" * 64
        packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(BridgeError):
            reader.load(descriptor, SOURCE_ID, bundle)


if __name__ == "__main__":
    unittest.main()
