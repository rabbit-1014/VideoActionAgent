"""Critical seams: fixed routing, scoped grants, single visual attempt and guarded publication."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, "C:/Users/泽/Documents/VideoActionAgent/src")
sys.path.insert(0, "D:/CodexWork/VideoActionAgent-downstream-20261014")
from bridge_contract import BridgeError
from video_action_agent.notes_service import NotesServiceError
from video_action_agent.source_candidate_bundle import read_bundle, save_bundle
from video_action_agent.source_capture import SourceCapture

from native_notes import NativeNotes
from vision_pipeline import VisionPipeline
from vision_policy import VisualIntents, visual_plan


class VisualPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temp.name)
        self.bundle = read_bundle(Path("D:/VideoActionAgent-asr-pilot/source-bundles/3b016931-dca8-4dea-9f3e-2e75890a8417"))
        self.material = self.bundle["source_envelope"]
        self.calls = []
        self.fail_vision = False
        self.cancel_vision = False
        self.intents = VisualIntents(self.root / "scopes", "alice")
        self.source_id, self.note_id = str(uuid4()), str(uuid4())
        self.request = "整理界面操作步骤，并保留有帮助的画面"

        def acquire(source, output, **kwargs):
            save_bundle(self.bundle, output)

        self.sources = SourceCapture("alice", self.root / "sources", capture_fn=acquire)
        self.sources.start(self.source_id, "https://www.bilibili.com/video/BV1gD421u75J?p=1", allow_local_audio_read=True)
        self.sources.wait(self.source_id, 3)

        def capture(material, times, output, *, checkpoint):
            import hashlib
            self.calls.append("frames")
            checkpoint()
            output.mkdir(parents=True, exist_ok=True)
            path = output / "f000.jpg"
            shutil.copyfile(Path("D:/CodexWork/VideoActionAgent-visual-20261014-01a1393d/frames/frame-102.jpg"), path)
            return {"source": {k: material["source_evidence"][k] for k in ("bvid", "page", "cid")},
                    "state": "available", "observed_at": "fixture", "frames": [
                        {"id": "f000", "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "actual_frame_pts_seconds": 102.023, "bytes": path.stat().st_size}]}

        def vision(prompt, frames):
            self.calls.append("vision")
            if self.cancel_vision:
                self.notes.ledger.cancel(self.note_id)
            if self.fail_vision:
                raise TimeoutError("unknown")
            return {"text": json.dumps({"frames": [{"frame_id": "f000", "visible_text": ["文稿匹配"],
                "observations": ["中央文本输入窗口"], "uncertain": []}], "limits": ["单帧不证明完整过程"]})}

        def generate(identity, material, request, style, client, output, *, extras_override=None):
            text = client.chat.completions.create(model="glm-5.3", messages=[{"role": "user", "content": extras_override or request}]).choices[0].message.content
            target = output / (identity + ".json")
            target.write_text(json.dumps({"markdown": text, "transcript": material["transcription"],
                                          "audio_meta": {"title": "示例"}}, ensure_ascii=False), encoding="utf-8")
            return target

        def text_model(prompt):
            self.calls.append("text")
            if "f000" in prompt:
                return "# 笔记\n\n## 方法\n\n打开文稿输入窗口。\n\n[[FRAME:f000]]"
            return "# 笔记\n\n仅依据声音整理，未读取画面。"

        self.pipeline = VisionPipeline(self.root / "vision", self.intents, generate,
                                       capture=capture, vision_transport=vision)
        self.notes = NativeNotes("alice", self.root / "notes", self.sources, self.pipeline.generate, text_model,
                                 result_guard=self.pipeline.validate_published)

    def bind(self, granted=True):
        return self.intents.bind(self.note_id, self.source_id,
            "https://www.bilibili.com/video/BV1gD421u75J?p=1", self.request,
            allow_local_video_read=granted, allow_model_images=granted)

    def run_note(self):
        self.notes.start(self.note_id, self.source_id, self.request, allow_model_text=True)
        self.notes.wait(self.note_id, 5)
        return self.notes.get(self.note_id)

    def tearDown(self):
        self.notes.close()
        self.sources.close()
        self.temp.cleanup()

    def test_fixed_light_route_and_scope_cannot_be_upgraded(self):
        plan = visual_plan(self.material, "只整理讲者口头内容，做简短摘要")
        self.assertFalse(plan["needed"])
        self.assertEqual(plan["times"], [])
        technical = visual_plan(self.material, "整理界面操作和代码细节")
        self.assertTrue(technical["needed"])
        self.assertTrue(1 <= len(technical["times"]) <= 3)
        self.bind(False)
        with self.assertRaises(BridgeError):
            self.bind(True)
        self.assertEqual(self.run_note()["state"], "available")
        self.assertEqual(self.calls, ["text"])

    def test_fresh_frames_into_native_note_once_and_asset_fenced(self):
        self.bind()
        self.assertEqual(self.run_note()["state"], "available")
        candidate = self.notes.candidate(self.note_id)
        self.assertIn("/bridge/assets/", candidate["result"]["markdown"])
        self.assertNotIn("[[FRAME:", candidate["result"]["markdown"])
        self.assertEqual(candidate["result"]["transcript"], self.material["transcription"])
        self.notes.start(self.note_id, self.source_id, self.request, allow_model_text=True)
        self.assertEqual(self.calls, ["frames", "vision", "text"])
        scope_path = self.intents.root / (self.note_id + ".json")
        original_scope = scope_path.read_bytes()
        scope = json.loads(original_scope)
        scope["allow_model_images"] = False
        scope_path.write_text(json.dumps(scope), encoding="utf-8")
        try:
            with self.assertRaises((BridgeError, NotesServiceError)):
                self.notes.candidate(self.note_id)
        finally:
            scope_path.write_bytes(original_scope)
        image = next((self.root / "vision" / self.note_id / "frames").glob("*.jpg"))
        image.write_bytes(image.read_bytes() + b"changed")
        with self.assertRaises((BridgeError, NotesServiceError)):
            self.pipeline.read_asset(self.notes, self.note_id, image.name)

    def test_unknown_visual_does_not_replay_and_cancel_blocks_late_note(self):
        self.bind()
        self.fail_vision = True
        self.assertEqual(self.run_note()["state"], "available")
        self.assertEqual(self.notes._row(self.note_id)["vision_state"], "outcome_unknown")
        self.notes.start(self.note_id, self.source_id, self.request, allow_model_text=True)
        self.assertEqual(self.calls, ["frames", "vision", "text"])
        self.note_id = str(uuid4())
        self.bind()
        self.fail_vision, self.cancel_vision = False, True
        self.assertEqual(self.run_note()["state"], "canceled")
        self.assertEqual(self.calls[-2:], ["frames", "vision"])


if __name__ == "__main__":
    unittest.main()
