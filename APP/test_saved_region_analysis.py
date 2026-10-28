"""Offline checks for one authorised, source-bound saved-frame region attempt."""
import copy
import hashlib
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, "D:/CodexWork/VideoActionAgent-downstream-20261014")
sys.path.insert(0, "D:/CodexWork/VideoActionAgent-visual-intake-20261015")
from bridge_contract import BridgeError, atomic, digest


class SavedRegionAnalysisTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("saved_region_analysis"),
                             "saved-frame region analysis has not been implemented")
        return importlib.import_module("saved_region_analysis")

    def fixture(self, directory):
        source = {"bvid": "BVregions", "page": 1, "cid": 42}
        descriptor = {"owner": "offline-owner", "source_id": "offline-source", "donor_id": "offline-donor",
                      "source_sha256": "a" * 64, "packet_sha256": "b" * 64, "scope_sha256": "c" * 64}
        binding = {"owner": "offline-owner", "source_id": "offline-source", "request_id": "offline-request",
                   "source_sha256": "a" * 64, "allow_model_images": True, "material_mode": "saved_visual",
                   "visual_material": descriptor}
        frames = []
        for index in range(2):
            path = directory / f"f{index:03}.jpg"
            Image.new("RGB", (1000, 500), (index * 100, 40, 90)).save(path, format="JPEG")
            frames.append({"id": f"f{index:03}", "path": str(path),
                           "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "width": 1000, "height": 500,
                           "actual_frame_pts_seconds": index + 1.0})
        snapshot = {"source": source, "source_sha256": "a" * 64, "frames": frames,
                    "reuse_descriptor": copy.deepcopy(descriptor), "scope_sha256": digest(binding),
                    "observation": {"observations": ["old free text must never survive"]}}
        atomic(directory / "visual-material.json", snapshot)
        boxes = [[[100, 50], [200, 50], [200, 100], [100, 100]],
                 [[450, 150], [550, 150], [550, 200], [450, 200]],
                 [[0, 200], [100, 200], [100, 250], [0, 250]],
                 [[600, 50], [700, 50], [700, 100], [600, 100]]]
        texts = ["editor text", "overlapping", "touching boundary", "preview text"]
        ocr, projected = [], []
        for frame in frames:
            candidates = [{"text": text, "box": box} for text, box in zip(texts, boxes)]
            ocr.append({"frame_id": frame["id"], "frame_sha256": frame["sha256"],
                        "state": "available", "lines": [{**c, "score": .99} for c in candidates]})
            projected.append({"frame_id": frame["id"], "frame_sha256": frame["sha256"],
                              "ocr_state": "available", "text_candidates": candidates})
        atomic(directory / "text-grounding.json", {
            "schema_version": 1, "source": source, "source_sha256": "a" * 64,
            "visual_manifest_sha256": hashlib.sha256((directory / "visual-material.json").read_bytes()).hexdigest(),
            "ocr": ocr, "generation_text_projection": {"schema_version": 1,
                "policy": "same_frame_ocr_text_intersection_v1", "role": "unverified_corroborated_text_candidates",
                "frames": projected, "limits": [], "quality_approved": False}, "quality_approved": False})
        material = {"source_evidence": source, "transcription": {"segments": [{"text": "unchanged source"}]}}
        answer = {"text": json.dumps({"frames": [
            {"frame_id": frame["id"], "regions": [
                {"role": "editor_interface", "bounds": [0, 0, .5, 1]},
                {"role": "preview_content", "bounds": [.4, 0, 1, .8]}]}
            for frame in frames]}), "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "model": "offline-region-model", "finish_reason": "stop"}
        return binding, material, snapshot, answer

    def repin_snapshot(self, directory, snapshot):
        atomic(directory / "visual-material.json", snapshot)
        grounding = json.loads((directory / "text-grounding.json").read_bytes())
        grounding["visual_manifest_sha256"] = hashlib.sha256((directory / "visual-material.json").read_bytes()).hexdigest()
        expected = {frame["id"]: frame["sha256"] for frame in snapshot["frames"]}
        for frame in grounding["ocr"] + grounding["generation_text_projection"]["frames"]:
            frame["frame_sha256"] = expected[frame["frame_id"]]
        atomic(directory / "text-grounding.json", grounding)

    def test_protocol3_exact_aliases_preserve_raw_roles_and_readback(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            binding, material, snapshot, answer = self.fixture(directory)
            binding["goal_policy"] = {"version": 13}
            snapshot["scope_sha256"] = digest(binding)
            self.repin_snapshot(directory, snapshot)
            original = copy.deepcopy((binding, material, snapshot))
            canonical = ["editor_interface", "preview_content", "overlay", "unknown"]
            aliases = ["编辑器界面", "预览内容", "覆盖层", "未知"]
            content = {"frames": [{"frame_id": frame["id"], "editor_interface_visible": None,
                "assignments": [{"text_index": number, "role": roles[number - 1]}
                                for number in (4, 2, 1, 3)]}
                for frame, roles in zip(snapshot["frames"], (aliases, canonical))]}
            answer = {**answer, "text": "```json\n" + json.dumps(content, ensure_ascii=False) + "\n```"}
            calls = []
            def transport(prompt, frames):
                calls.append(True)
                self.assertIn("协议3", prompt)
                for alias, role in zip(aliases, canonical):
                    self.assertIn(alias + "→" + role, prompt)
                self.assertIn("画面容器/空间层", prompt)
                self.assertIn("预览内部的字幕、标题", prompt)
                self.assertTrue(all(isinstance(frame["image_bytes"], bytes) for frame in frames))
                return answer
            projection, manifest_sha, _ = module.analyse_saved_regions(
                directory, binding, material, snapshot, checkpoint=lambda: None, transport=transport)
            self.assertEqual(calls, [True])
            self.assertEqual((binding, material, snapshot), original)
            self.assertEqual(projection["schema_version"], 3)
            self.assertEqual(projection["policy"], "saved_frame_candidate_layers_v3")
            self.assertFalse(projection["quality_approved"])
            for frame, raw_roles, provenance in zip(projection["frames"], (aliases, canonical),
                    ("exact_chinese_alias_v1", "canonical_english_v1")):
                items = frame["text_candidates"]
                self.assertEqual([item["role"] for item in items], canonical)
                self.assertEqual([item["raw_role"] for item in items], raw_roles)
                self.assertTrue(all(item["role_normalization_source"] == provenance for item in items))
                self.assertTrue(all(item["role_provenance"] == "unverified_model_candidate_layer_assignment"
                                    for item in items))
                self.assertTrue(all("region_index" not in item for item in items))
            self.assertEqual(json.loads((directory / "saved-region-raw-reply.json").read_bytes()), answer)
            for filename in ("saved-region-request.json", "saved-region-attempt.json", "saved-region-manifest.json"):
                self.assertEqual(json.loads((directory / filename).read_bytes())["analysis_protocol"], 3)
            row = {"owner": binding["owner"], "source_id": binding["source_id"],
                   "source_sha256": binding["source_sha256"], "request_id": binding["request_id"],
                   "saved_region_manifest_sha256": manifest_sha}
            self.assertEqual(module.verify_saved_regions(directory, row), projection)
            candidates = module._grounding(directory, snapshot,
                hashlib.sha256((directory / "visual-material.json").read_bytes()).hexdigest())
            with self.assertRaisesRegex(BridgeError, "candidate_assignment_invalid"):
                module._parse2(answer, snapshot["frames"], candidates)

    def test_protocol3_unknown_roles_and_incomplete_candidates_remain_rejected(self):
        module = self.module()
        for defect in ("unknown_word", "spaced_role", "duplicate", "missing"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, answer = self.fixture(directory)
                binding["goal_policy"] = {"version": 13}
                snapshot["scope_sha256"] = digest(binding)
                self.repin_snapshot(directory, snapshot)
                content = {"frames": [{"frame_id": frame["id"], "assignments": [
                    {"text_index": number, "role": "未知"} for number in range(1, 5)]}
                    for frame in snapshot["frames"]]}
                items = content["frames"][0]["assignments"]
                if defect == "unknown_word":
                    items[0]["role"] = "预览"
                elif defect == "spaced_role":
                    items[0]["role"] = " 覆盖层"
                elif defect == "duplicate":
                    items[-1]["text_index"] = 1
                else:
                    items.pop()
                answer = {**answer, "text": json.dumps(content, ensure_ascii=False)}
                calls = []
                with self.assertRaises(BridgeError):
                    module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=lambda: None,
                                                 transport=lambda *_: (calls.append(True) or answer))
                self.assertEqual(calls, [True])
                attempt = json.loads((directory / "saved-region-attempt.json").read_bytes())
                self.assertEqual(attempt["analysis_protocol"], 3)
                self.assertEqual(attempt["state"], "received_invalid")
                self.assertTrue(attempt["received"])
                self.assertEqual(json.loads((directory / "saved-region-raw-reply.json").read_bytes()), answer)
                self.assertFalse((directory / "saved-region-manifest.json").exists())
                with self.assertRaisesRegex(BridgeError, "saved_region_previous_attempt_exists"):
                    module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=lambda: None,
                                                 transport=lambda *_: self.fail("repeat dispatch"))

    def test_candidate_protocol2_and_historical_protocol1_readback(self):
        module = self.module()
        for mode in ("assignments", "fenced_assignments", "policy12_assignments", "historical_regions"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, answer = self.fixture(directory)
                binding["goal_policy"] = {"version": 10 if mode == "historical_regions" else
                                          12 if mode == "policy12_assignments" else 11}
                snapshot["scope_sha256"] = digest(binding)
                self.repin_snapshot(directory, snapshot)
                roles = ["editor_interface", "overlay", "unknown", "preview_content"]
                if mode != "historical_regions":
                    content = {"frames": [{"frame_id": frame["id"],
                        "editor_interface_visible": True if index == 0 else None,
                        "assignments": [{"text_index": number, "role": roles[number - 1]}
                                        for number in (4, 2, 1, 3)]}
                        for index, frame in enumerate(snapshot["frames"])]}
                    text = json.dumps(content)
                    answer = {**answer, "text": "```json\n" + text + "\n```" if mode == "fenced_assignments" else text}
                calls = []
                def transport(prompt, frames):
                    calls.append(True)
                    if mode != "historical_regions":
                        self.assertIn('"text_index": 1', prompt)
                        self.assertNotIn('"regions":', prompt)
                        self.assertTrue(all(isinstance(frame["image_bytes"], bytes) for frame in frames))
                    return answer
                projection, manifest_sha, metrics = module.analyse_saved_regions(
                    directory, binding, material, snapshot, checkpoint=lambda: None, transport=transport)
                self.assertEqual(calls, [True])
                self.assertEqual(metrics["usage"], answer["usage"])
                row = {"owner": binding["owner"], "source_id": binding["source_id"],
                       "source_sha256": binding["source_sha256"], "request_id": binding["request_id"],
                       "saved_region_manifest_sha256": manifest_sha}
                manifest_path = directory / "saved-region-manifest.json"
                manifest = json.loads(manifest_path.read_bytes())
                if mode == "historical_regions":
                    # A genuine old accepted v1 record had no protocol tag.
                    # Recreate that shape only within this temporary fixture.
                    for name in ("request", "attempt"):
                        path = directory / manifest["references"][name]["filename"]
                        value = json.loads(path.read_bytes())
                        value.pop("analysis_protocol", None)
                        atomic(path, value)
                        manifest["references"][name]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                    manifest.pop("analysis_protocol", None)
                    atomic(manifest_path, manifest)
                    row["saved_region_manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
                    self.assertEqual(projection["schema_version"], 1)
                    self.assertIn("regions", projection["frames"][0])
                    with self.assertRaises(BridgeError):
                        module._parse({**answer, "text": "```json\n" + answer["text"] + "\n```"}, snapshot["frames"])
                else:
                    self.assertEqual(manifest["analysis_protocol"], 2)
                    for name in ("request", "attempt"):
                        value = json.loads((directory / manifest["references"][name]["filename"]).read_bytes())
                        self.assertEqual(value["analysis_protocol"], 2)
                    self.assertEqual(projection["schema_version"], 2)
                    self.assertNotIn("regions", projection["frames"][0])
                    self.assertTrue(projection["frames"][0]["editor_interface_visible"])
                    self.assertIsNone(projection["frames"][1]["editor_interface_visible"])
                    grounding = json.loads((directory / "text-grounding.json").read_bytes())
                    for frame, original in zip(projection["frames"], grounding["generation_text_projection"]["frames"]):
                        self.assertEqual([item["role"] for item in frame["text_candidates"]], roles)
                        self.assertEqual([item["text_index"] for item in frame["text_candidates"]], [1, 2, 3, 4])
                        self.assertEqual([item["box"] for item in frame["text_candidates"]],
                                         [item["box"] for item in original["text_candidates"]])
                        self.assertTrue(all("region_index" not in item for item in frame["text_candidates"]))
                    self.assertFalse(projection["quality_approved"])
                self.assertEqual(module.verify_saved_regions(directory, row), projection)

    def test_protocol2_bad_candidate_indices_are_retained_without_replay(self):
        module = self.module()
        for defect in ("missing", "duplicate", "out_of_range"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, answer = self.fixture(directory)
                binding["goal_policy"] = {"version": 11}
                snapshot["scope_sha256"] = digest(binding)
                self.repin_snapshot(directory, snapshot)
                content = {"frames": [{"frame_id": frame["id"], "assignments": [
                    {"text_index": number, "role": "unknown"} for number in range(1, 5)]}
                    for frame in snapshot["frames"]]}
                items = content["frames"][0]["assignments"]
                if defect == "missing":
                    items.pop()
                else:
                    items[-1]["text_index"] = 1 if defect == "duplicate" else 5
                answer = {**answer, "text": json.dumps(content)}
                calls = []
                with self.assertRaises(BridgeError):
                    module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=lambda: None,
                                                 transport=lambda *_: (calls.append(True) or answer))
                self.assertEqual(calls, [True])
                attempt = json.loads((directory / "saved-region-attempt.json").read_bytes())
                self.assertEqual(attempt["analysis_protocol"], 2)
                self.assertEqual(attempt["state"], "received_invalid")
                self.assertTrue(attempt["received"])
                self.assertEqual(json.loads((directory / "saved-region-raw-reply.json").read_bytes()), answer)
                self.assertFalse((directory / "saved-region-manifest.json").exists())
                with self.assertRaisesRegex(BridgeError, "saved_region_previous_attempt_exists"):
                    module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=lambda: None,
                                                 transport=lambda *_: self.fail("repeat dispatch"))

    def test_default_uses_the_saved_region_adapter(self):
        module = self.module()
        adapter = importlib.import_module("saved_region_transport")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            binding, material, snapshot, answer = self.fixture(directory)
            calls = []
            def transport(prompt, frames):
                calls.append((prompt, frames))
                return answer
            legacy = SimpleNamespace(vision_transport=lambda *_: self.fail("legacy transport selected"))
            with patch.object(adapter, "transport", side_effect=transport), patch.dict(sys.modules, {"vision_pipeline": legacy}):
                module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=lambda: None)
            self.assertEqual(len(calls), 1)

    def test_transport_receives_fixed_image_bytes_even_when_the_path_changes(self):
        module = self.module()
        adapter = importlib.import_module("saved_region_transport")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            binding, material, snapshot, answer = self.fixture(directory)
            original = {frame["id"]: Path(frame["path"]).read_bytes() for frame in snapshot["frames"]}
            sent = []
            def transport(prompt, frames):
                for frame in frames:
                    self.assertEqual(frame.get("image_bytes"), original[frame["id"]])
                    Path(frame["path"]).write_bytes(b"changed after validation")
                # The real adapter must still encode the pinned bytes without
                # reopening a path; publication then rejects the changed source.
                sent.extend(adapter._content(prompt, frames))
                return answer
            with self.assertRaisesRegex(BridgeError, "saved_region_frame_changed"):
                module.analyse_saved_regions(directory, binding, material, snapshot,
                                             checkpoint=lambda: None, transport=transport)
            self.assertEqual(sum(item["type"] == "image_url" for item in sent), 2)
            attempt = json.loads((directory / "saved-region-attempt.json").read_bytes())
            self.assertTrue(attempt["received"])
            self.assertTrue(attempt["network_dispatched"])
            self.assertFalse((directory / "saved-region-manifest.json").exists())

    def test_known_predispatch_failure_is_zero_and_never_replayed(self):
        module = self.module()
        adapter = importlib.import_module("saved_region_transport")
        for code in ("saved_region_key_unavailable", "saved_region_transport_frame_hash_mismatch"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, _ = self.fixture(directory)
                def transport(*_):
                    raise adapter.RegionTransportError(code)
                with self.assertRaises(BridgeError):
                    module.analyse_saved_regions(directory, binding, material, snapshot,
                                                 checkpoint=lambda: None, transport=transport)
                attempt = json.loads((directory / "saved-region-attempt.json").read_bytes())
                self.assertEqual(attempt["state"], "not_dispatched")
                self.assertEqual(attempt["calls"], 0)
                self.assertEqual(attempt["new_visual_calls"], 0)
                self.assertFalse(attempt["network_dispatched"])
                self.assertFalse(attempt["received"])
                with self.assertRaisesRegex(BridgeError, "saved_region_previous_attempt_exists"):
                    module.analyse_saved_regions(directory, binding, material, snapshot,
                        checkpoint=lambda: None, transport=lambda *_: self.fail("repeat dispatch"))

    def test_invalid_rejected_and_unknown_receipts_remain_distinct(self):
        module = self.module()
        adapter = importlib.import_module("saved_region_transport")
        for state in ("received_invalid", "rejected", "outcome_unknown"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, answer = self.fixture(directory)
                receipt = {**answer, "received": True, "network_dispatched": True,
                           "transport_state": state, "raw_body_base64": "YmFk"}
                def transport(*_):
                    if state == "outcome_unknown":
                        raise adapter.RegionTransportError("saved_region_transport_outcome_unknown", network_dispatched=True)
                    return receipt
                with self.assertRaises(BridgeError):
                    module.analyse_saved_regions(directory, binding, material, snapshot,
                                                 checkpoint=lambda: None, transport=transport)
                attempt = json.loads((directory / "saved-region-attempt.json").read_bytes())
                self.assertEqual(attempt["state"], state)
                self.assertEqual(attempt["calls"], 1)
                self.assertTrue(attempt["network_dispatched"])
                self.assertEqual(attempt["received"], state != "outcome_unknown")
                self.assertGreaterEqual(attempt["elapsed_seconds"], 0)
                self.assertFalse((directory / "saved-region-manifest.json").exists())
                if state != "outcome_unknown":
                    self.assertEqual(attempt["usage"], answer["usage"])
                    self.assertEqual(json.loads((directory / "saved-region-raw-reply.json").read_bytes()), receipt)

    def test_bounded_large_raw_receipt_remains_verifiable(self):
        module = self.module()
        adapter = importlib.import_module("saved_region_transport")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            binding, material, snapshot, answer = self.fixture(directory)
            body = json.dumps({"choices": [{"message": {"content": " " * 1_800_000 + answer["text"]},
                                            "finish_reason": "stop"}]}).encode()
            receipt = adapter._receipt(body, "offline-secret")
            projection, manifest_sha, _ = module.analyse_saved_regions(
                directory, binding, material, snapshot, checkpoint=lambda: None, transport=lambda *_: receipt)
            self.assertEqual(module.verify_saved_regions(directory, {
                "owner": binding["owner"], "source_id": binding["source_id"],
                "source_sha256": binding["source_sha256"], "request_id": binding["request_id"],
                "saved_region_manifest_sha256": manifest_sha}), projection)

    def test_real_image_metadata_is_checked_and_transport_paths_are_absolute(self):
        module = self.module()
        for defect in ("wrong_dimensions", "not_an_image", "unsupported_format", "truncated_image"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, answer = self.fixture(directory)
                frame = snapshot["frames"][0]
                if defect == "wrong_dimensions":
                    frame["width"] = 100
                else:
                    path = Path(frame["path"])
                    if defect == "not_an_image":
                        path.write_bytes(b"not an image")
                    elif defect == "truncated_image":
                        path.write_bytes(path.read_bytes()[:-100])
                    else:
                        Image.new("RGB", (1000, 500)).save(path, format="GIF")
                    frame["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                self.repin_snapshot(directory, snapshot)
                calls = []
                with self.assertRaises(BridgeError):
                    module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=lambda: None,
                                                 transport=lambda *_: (calls.append(True) or answer))
                self.assertEqual(calls, [], "invalid image must be rejected before dispatch")
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            binding, material, snapshot, answer = self.fixture(directory)
            for frame in snapshot["frames"]:
                frame["path"] = Path(frame["path"]).name
            self.repin_snapshot(directory, snapshot)
            original = copy.deepcopy(snapshot)
            sent = []
            module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=lambda: None,
                                         transport=lambda _, frames: (sent.extend(frames) or answer))
            self.assertEqual(snapshot, original)
            for frame in sent:
                self.assertTrue(Path(frame["path"]).is_absolute())
                self.assertEqual(hashlib.sha256(Path(frame["path"]).read_bytes()).hexdigest(), frame["sha256"])

    def test_missing_or_invalid_crossed_projection_never_dispatches(self):
        module = self.module()
        for defect in ("missing", "invalid", "missing_frame", "approved"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, answer = self.fixture(directory)
                path = directory / "text-grounding.json"
                grounding = json.loads(path.read_bytes())
                if defect == "missing":
                    grounding.pop("generation_text_projection")
                elif defect == "invalid":
                    grounding["generation_text_projection"] = []
                elif defect == "missing_frame":
                    grounding["generation_text_projection"]["frames"].pop()
                else:
                    grounding["generation_text_projection"]["quality_approved"] = True
                atomic(path, grounding)
                calls = []
                with self.assertRaises(BridgeError):
                    module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=lambda: None,
                                                 transport=lambda *_: (calls.append(True) or answer))
                self.assertEqual(calls, [], "uncrossed OCR must be rejected before dispatch")
                self.assertFalse((directory / "saved-region-attempt.json").exists())

    def test_input_snapshot_owner_scope_and_late_cancellation_are_enforced(self):
        module = self.module()
        for defect in ("wrong_owner", "wrong_scope", "wrong_descriptor"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, answer = self.fixture(directory)
                if defect == "wrong_owner":
                    snapshot["reuse_descriptor"]["owner"] = "another-owner"
                elif defect == "wrong_scope":
                    snapshot["scope_sha256"] = "d" * 64
                else:
                    snapshot["reuse_descriptor"]["donor_id"] = "another-donor"
                self.repin_snapshot(directory, snapshot)
                calls = []
                with self.assertRaises(BridgeError):
                    module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=lambda: None,
                                                 transport=lambda *_: (calls.append(True) or answer))
                self.assertEqual(calls, [], "unowned/stale snapshot must be rejected before dispatch")
        for when in ("after_input_validation", "after_projection", "after_manifest"):
            with self.subTest(when=when), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, answer = self.fixture(directory)
                cancelled, calls, validations = [], [], []
                def checkpoint():
                    if cancelled:
                        raise BridgeError("cancelled")
                original_frames, original_projection = module._frames, module._projection
                def frames(*args, **kwargs):
                    result = original_frames(*args, **kwargs)
                    validations.append(True)
                    if when == "after_input_validation" and len(validations) == 2:
                        cancelled.append(True)
                    return result
                def projection(*args, **kwargs):
                    result = original_projection(*args, **kwargs)
                    if when == "after_projection":
                        cancelled.append(True)
                    return result
                def write(path, value):
                    atomic(path, value)
                    if when == "after_manifest" and path.name == "saved-region-manifest.json":
                        cancelled.append(True)
                with patch.object(module, "_frames", side_effect=frames), \
                        patch.object(module, "_projection", side_effect=projection), patch.object(module, "atomic", side_effect=write):
                    with self.assertRaisesRegex(BridgeError, "cancelled"):
                        module.analyse_saved_regions(directory, binding, material, snapshot, checkpoint=checkpoint,
                            transport=lambda *_: (calls.append(True) or answer))
                self.assertEqual(len(calls), 0 if when == "after_input_validation" else 1)
                self.assertFalse((directory / "saved-region-manifest.json").exists())

    def test_no_image_permission_rejects_before_io_or_transport(self):
        module = self.module()
        for binding in ({}, {"allow_model_images": 1, "material_mode": "saved_visual"},
                        {"allow_model_images": True, "material_mode": "audio_only"}):
            with self.subTest(binding=binding), patch.object(Path, "read_bytes", side_effect=AssertionError("I/O")), \
                    patch.object(Path, "exists", side_effect=AssertionError("I/O")):
                with self.assertRaisesRegex(BridgeError, "saved_region_not_authorized"):
                    module.analyse_saved_regions("not-read", binding, {}, {}, checkpoint=lambda: self.fail("checkpoint"),
                                                 transport=lambda *_: self.fail("transport"))

    def test_regions_assign_only_unique_contained_ocr_and_bind_saved_evidence(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            binding, material, snapshot, answer = self.fixture(directory)
            original = copy.deepcopy((binding, material, snapshot))
            calls, checks = [], []
            def transport(prompt, frames):
                calls.append((prompt, copy.deepcopy(frames)))
                self.assertNotIn("old free text", prompt)
                return answer
            projection, manifest_sha, metrics = module.analyse_saved_regions(
                directory, binding, material, snapshot, checkpoint=lambda: checks.append(True), transport=transport)
            self.assertEqual(len(calls), 1)
            self.assertGreaterEqual(len(checks), 2)
            self.assertEqual((binding, material, snapshot), original)
            self.assertEqual([f["id"] for f in calls[0][1]], ["f000", "f001"])
            self.assertNotIn('"observations":', json.dumps(projection))
            self.assertNotIn("old free text", json.dumps(projection))
            self.assertFalse(projection["quality_approved"])
            for frame in projection["frames"]:
                self.assertEqual([c["role"] for c in frame["text_candidates"]],
                                 ["editor_interface", "unknown", "unknown", "preview_content"])
            self.assertEqual(metrics["usage"], answer["usage"])
            self.assertGreaterEqual(metrics["elapsed_seconds"], 0)
            row = {"owner": binding["owner"], "master_id": binding["source_id"],
                   "master_sha256": binding["source_sha256"], "request_id": binding["request_id"],
                   "saved_region_manifest_sha256": manifest_sha}
            self.assertEqual(module.verify_saved_regions(directory, row), projection)
            for changes in ({"owner": "another"}, {"master_sha256": "b" * 64}):
                with self.subTest(changes=changes), self.assertRaises(BridgeError):
                    module.verify_saved_regions(directory, {**row, **changes})
            manifest = json.loads((directory / "saved-region-manifest.json").read_bytes())
            for ref in manifest["references"].values():
                self.assertEqual(hashlib.sha256((directory / ref["filename"]).read_bytes()).hexdigest(), ref["sha256"])
            Path(snapshot["frames"][0]["path"]).write_bytes(b"changed")
            with self.assertRaises(BridgeError):
                module.verify_saved_regions(directory, row)

    def test_attempt_cancel_change_and_transport_failure_never_redispatch(self):
        module = self.module()
        for scenario in ("old_attempt", "cancel_before", "cancel_after", "changed_before", "changed_after", "invalid", "unknown"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                binding, material, snapshot, answer = self.fixture(directory)
                calls, checks = [], []
                if scenario == "old_attempt":
                    atomic(directory / "saved-region-attempt.json", {"state": "outcome_unknown"})
                if scenario == "changed_before":
                    Path(snapshot["frames"][0]["path"]).write_bytes(b"changed")
                def checkpoint():
                    checks.append(True)
                    if scenario == "cancel_before" or (scenario == "cancel_after" and calls):
                        raise BridgeError("cancelled")
                def transport(*_):
                    calls.append(True)
                    if scenario == "unknown":
                        raise TimeoutError("offline outcome unknown")
                    if scenario == "changed_after":
                        Path(snapshot["frames"][0]["path"]).write_bytes(b"changed")
                    return {**answer, "text": "{\"frames\":[]}"} if scenario == "invalid" else answer
                with self.assertRaises((BridgeError, TimeoutError)):
                    module.analyse_saved_regions(directory, binding, material, snapshot,
                                                 checkpoint=checkpoint, transport=transport)
                expected_calls = 0 if scenario in {"old_attempt", "cancel_before", "changed_before"} else 1
                self.assertEqual(len(calls), expected_calls)
                if expected_calls:
                    self.assertGreaterEqual(len(checks), 3, "dispatched attempts require a post-transport checkpoint")
                    attempt = json.loads((directory / "saved-region-attempt.json").read_bytes())
                    self.assertEqual(attempt["state"], "outcome_unknown" if scenario == "unknown" else "received_invalid")
                    self.assertEqual(attempt["calls"], 1)
                    self.assertFalse(attempt["automatic_retry"])
                    self.assertFalse((directory / "saved-region-manifest.json").exists())
                    with self.assertRaises(BridgeError):
                        module.analyse_saved_regions(directory, binding, material, snapshot,
                            checkpoint=lambda: None, transport=lambda *_: self.fail("repeat dispatch"))
                    if scenario != "unknown":
                        self.assertEqual(json.loads((directory / "saved-region-raw-reply.json").read_bytes()),
                                         {**answer, "text": "{\"frames\":[]}"} if scenario == "invalid" else answer)


if __name__ == "__main__":
    unittest.main()
