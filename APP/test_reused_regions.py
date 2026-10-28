"""Offline integration checks through the real reused native generation path."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

sys.path[:0] = [
    "C:/Users/泽/Documents/VideoActionAgent/src",
    "D:/CodexWork/VideoActionAgent-downstream-20261014",
    "D:/CodexWork/VideoActionAgent-visual-intake-20261015",
]

from bridge_contract import BridgeError, atomic
from native_purposes import profile
from reused_native import ReusedNative
from saved_region_transport import RegionTransportError


class _Ledger:
    def __init__(self, row):
        self.row = row

    def get(self, identity):
        assert identity == self.row["request_id"]
        return copy.deepcopy(self.row)

    def update(self, identity, **values):
        assert identity == self.row["request_id"]
        self.row.update(copy.deepcopy(values))


class ReusedRegionsIntegrationTests(unittest.TestCase):
    def test_generation_receives_readable_layer_observations_without_internal_enums(self):
        from native_purposes import extras
        binding = {'native_profile': profile('source_answer'), 'output_kind': 'source_answer',
                   'user_request': '只区分画面层级，不给操作步骤，不插图', 'goal_policy': {'version': 14}}
        regions = {'schema_version': 3, 'source_sha256': 'a'*64, 'frames': [
            {'frame_id': 'f000', 'editor_interface_visible': True, 'text_candidates': [
                {'text': '片内字幕', 'role': 'preview_content', 'raw_role': '预览内容',
                 'role_normalization_source': 'exact_chinese_alias_v1', 'bounds': [0.1,0.2,0.4,0.5]}]}]}
        original = copy.deepcopy(regions)
        text = extras(binding, packet={'generation_region_projection': regions})
        self.assertIn('片内字幕', text)
        self.assertIn('素材预览', text)
        self.assertNotIn('preview_content', text)
        self.assertNotIn('exact_chinese_alias_v1', text)
        self.assertNotIn('a'*64, text)
        self.assertEqual(regions, original)
        binding['goal_policy']['version'] = 13
        self.assertIn('preview_content', extras(binding, packet={'generation_region_projection': regions}))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.identity = "offline-reused-regions"
        self.events = []
        self.previous = None
        self.source = {"bvid": "BVoffline", "page": 1, "cid": 42}
        descriptor = {"owner": "offline-owner", "source_id": "offline-source",
                      "donor_id": "offline-donor", "source_sha256": "a" * 64,
                      "packet_sha256": "b" * 64, "scope_sha256": "c" * 64}
        self.binding = {"owner": "offline-owner", "source_id": "offline-source",
                        "request_id": self.identity, "source_sha256": "a" * 64,
                        "allow_model_images": True, "material_mode": "saved_visual",
                        "visual_material": descriptor, "goal_policy": {"version": 8},
                        "output_kind": "source_answer", "user_request": "说明当前区域",
                        "native_profile": profile("source_answer")}
        donor = self.root / "donor.jpg"
        Image.new("RGB", (100, 50), (40, 90, 120)).save(donor, format="JPEG")
        self.packet = {"source": self.source, "source_sha256": "a" * 64,
                       "scope_sha256": "c" * 64,
                       "frames": [{"id": "f001", "path": str(donor),
                                   "sha256": hashlib.sha256(donor.read_bytes()).hexdigest(),
                                   "width": 100, "height": 50,
                                   "actual_frame_pts_seconds": 1.0}],
                       "observation": {"frames": [{"frame_id": "f001", "visible_text": ["KEY"],
                           "observations": ["old free text must never survive"], "uncertain": []}]}}
        self.material = {"source_evidence": self.source,
                         "transcription": {"segments": [{"text": "source remains unchanged"}]}}
        self.ledger = _Ledger({"request_id": self.identity, "master_id": "offline-source",
                               "source_id": "offline-source", "master_sha256": "a" * 64,
                               "owner": "offline-owner", "state": "running"})
        self.client = SimpleNamespace(service=SimpleNamespace(ledger=self.ledger),
                                      purpose_prompt_version=2,
                                      chat=SimpleNamespace(completions=SimpleNamespace(create=self.text)))
        self.pipeline = ReusedNative(self.root / "reused", self.context, self.native)
        self.pipeline.text_reader = self.ocr
        self.pipeline.region_transport = self.regions
        self.output = self.root / "output"
        self.output.mkdir()

    def context(self, identity):
        self.assertEqual(identity, self.identity)
        return copy.deepcopy(self.binding), copy.deepcopy(self.packet), self.previous

    def ocr(self, frames):
        self.events.append("ocr")
        return [{"frame_id": frame["id"], "frame_sha256": frame["sha256"],
                 "state": "available", "lines": [{"text": "KEY", "box": [5, 5, 25, 15],
                                                    "score": .99}]} for frame in frames]

    def regions(self, prompt, frames):
        self.events.append("regions")
        self.assertIn("text_grounding_manifest_sha256", self.ledger.row)
        self.assertNotIn("saved_region_manifest_sha256", self.ledger.row)
        self.assertEqual(frames[0]["image_bytes"], (self.root / "donor.jpg").read_bytes())
        return {"text": json.dumps({"frames": [{"frame_id": "f001", "regions": [
                    {"role": "editor_interface", "bounds": [0, 0, .5, 1]}]}]}),
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                "model": "offline-only", "finish_reason": "stop"}

    def text(self, **kwargs):
        self.events.append("text")
        if "regions" in self.events:
            self.assertIn("saved_region_manifest_sha256", self.ledger.row)
            self.pipeline.validate_published(self.ledger.get(self.identity))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))])

    def native(self, identity, material, request, style, client, output, **kwargs):
        self.events.append("native")
        self.hint = kwargs["extras_override"]
        self.assertNotIn("old free text must never survive", self.hint)
        client.chat.completions.create(messages=[{"role": "user", "content": self.hint}])
        target = output / (identity + ".json")
        atomic(target, {"markdown": "# Offline answer", "transcript": material["transcription"]})
        return target

    def generate(self):
        return self.pipeline.generate(self.identity, self.material, self.binding["user_request"],
                                      "minimal", self.client, self.output)

    def test_success_pins_region_manifest_before_native_and_cold_read_rejects_tamper(self):
        path = self.generate()
        self.assertEqual(self.events, ["ocr", "regions", "native", "text"])
        self.assertEqual(json.loads(path.read_bytes())["markdown"], "# Offline answer")
        self.assertEqual(self.ledger.row["saved_region_state"], "available")
        self.assertEqual(self.ledger.row["vision_model_calls"], 1)
        self.assertIn("unverified_visual_model_region_observations", self.hint)
        cold = ReusedNative(self.pipeline.root, self.context, self.native)
        cold.validate_published(self.ledger.get(self.identity))
        reply = self.pipeline.root / self.identity / "saved-region-raw-reply.json"
        reply.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(BridgeError, "saved_region_reference_changed"):
            cold.validate_published(self.ledger.get(self.identity))

    def test_opt_out_old_policy_and_revision_keep_legacy_text_path_without_image_dispatch(self):
        cases = ("opt_out", "old_policy", "revision")
        for case in cases:
            with self.subTest(case=case):
                if case != cases[0]:
                    self.setUp()
                if case == "opt_out":
                    self.binding["allow_model_images"] = False
                elif case == "old_policy":
                    self.binding["goal_policy"]["version"] = 7
                else:
                    self.binding["revision_request"] = {"instruction": "仅修表达", "parent_job_id": "parent"}
                    self.previous = "# Previous answer"
                self.generate()
                self.assertEqual(self.events, ["ocr", "native", "text"])
                self.assertEqual(self.ledger.row["vision_model_calls"], 0)
                self.assertNotIn("saved_region_manifest_sha256", self.ledger.row)
                self.assertFalse((self.pipeline.root / self.identity / "saved-region-attempt.json").exists())
                self.pipeline.validate_published(self.ledger.get(self.identity))
                self.assertIn("本次没有重新取帧或调用视觉模型", self.hint)

    def test_cancel_after_local_ocr_stops_before_any_region_or_native_dispatch(self):
        reader = self.pipeline.text_reader

        def cancel(frames):
            result = reader(frames)
            self.ledger.row["state"] = "cancelled"
            return result

        self.pipeline.text_reader = cancel
        with self.assertRaisesRegex(BridgeError, "cancelled"):
            self.generate()
        self.assertEqual(self.events, ["ocr"])
        self.assertNotIn("saved_region_manifest_sha256", self.ledger.row)

    def test_scope_change_after_region_receipt_records_call_and_stops_native(self):
        transport = self.pipeline.region_transport

        def change_scope(prompt, frames):
            answer = transport(prompt, frames)
            self.binding["user_request"] = "changed after dispatch"
            return answer

        self.pipeline.region_transport = change_scope
        with self.assertRaisesRegex(BridgeError, "purpose_scope_changed"):
            self.generate()
        self.assertEqual(self.events, ["ocr", "regions"])
        self.assertEqual(self.ledger.row["vision_model_calls"], 1)
        self.assertEqual(self.ledger.row["saved_region_state"], "received_invalid")
        self.assertTrue(self.ledger.row["saved_region_metrics"]["received"])
        self.assertNotIn("saved_region_manifest_sha256", self.ledger.row)
        self.assertFalse((self.pipeline.root / self.identity / "saved-region-manifest.json").exists())

    def test_preparation_and_unknown_failures_propagate_counts_without_native_or_replay(self):
        for dispatched in (False, True):
            with self.subTest(network_dispatched=dispatched):
                if dispatched:
                    self.setUp()

                def fail(prompt, frames, *, dispatched=dispatched):
                    self.events.append("regions")
                    raise RegionTransportError("offline_failure", network_dispatched=dispatched)

                self.pipeline.region_transport = fail
                with self.assertRaisesRegex(BridgeError, "saved_region_(not_dispatched|outcome_unknown)"):
                    self.generate()
                self.assertEqual(self.events, ["ocr", "regions"])
                self.assertEqual(self.ledger.row["vision_model_calls"], int(dispatched))
                self.assertEqual(self.ledger.row["saved_region_state"],
                                 "outcome_unknown" if dispatched else "not_dispatched")
                self.assertFalse(self.ledger.row["saved_region_metrics"]["received"])
                with self.assertRaises(FileExistsError):
                    self.generate()
                self.assertEqual(self.events, ["ocr", "regions"])


if __name__ == "__main__":
    unittest.main()
