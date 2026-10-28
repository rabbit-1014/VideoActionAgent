"""Critical same-source purpose, once-only model and current-material boundaries."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

sys.path[:0] = ["C:/Users/泽/Documents/VideoActionAgent/src", "D:/CodexWork/VideoActionAgent-downstream-20261014",
               "D:/CodexWork/VideoActionAgent-visual-intake-20261015"]
from video_action_agent.notes_service import NotesServiceError
from video_action_agent.source_candidate_bundle import read_bundle, save_bundle
from video_action_agent.source_capture import SourceCapture

from image_filter import eligible_frame_ids
from purpose_outputs import PurposeOutputs


class PurposeOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        bundle = read_bundle(Path("D:/CodexWork/VideoActionAgent-visual-intake-20261015/runtime/sources/8db58bff-b71b-5821-b129-b273bb2d7a68"))
        self.source = str(uuid4())
        self.calls, self.fail_model, self.cancel = [], False, False
        def acquire(url, output, **kwargs):
            self.calls.append("source")
            save_bundle(bundle, output)
        self.sources = SourceCapture("alice", self.root / "sources", capture_fn=acquire)
        self.sources.start(self.source, "https://www.bilibili.com/video/BV1AJen6SEVQ?p=42", allow_local_audio_read=True)
        self.sources.wait(self.source, 3)
        self.calls.clear()
        outer = self
        class Provider:
            model_id = "test-model"
            def complete(self, system, user):
                outer.calls.append("model")
                if outer.fail_model:
                    raise TimeoutError()
                if outer.cancel:
                    outer.outputs.cancel(outer.identity)
                data = json.loads(user)
                if "source_id" in data:
                    return json.dumps({"source_id": data["source_id"], "request_id": data["request_id"],
                        "source_status": "answered", "answer": [{"text": "这一课处理Markdown分块。", "segment_indices": [1]}],
                        "missing": [], "unverified": []})
                span = data["segments"][0]
                return json.dumps({"user_request": data["user_request"], "source_ref": data["source_ref"],
                    "payload_hash": data["payload_hash"], "methods": [{"title": "准备", "applicability": "上一课已完成",
                    "steps": [{"order": 1, "text": "处理Markdown文件。", "segment_indices": [1],
                               "start": span["start"], "end": span["end"]}],
                    "checkpoints": [{"text": "已处理文件", "segment_indices": [1],
                                     "start": span["start"], "end": span["end"]}]},
                                     {"unverified": ["具体代码未取得"]}]})
        self.factory = Provider
        self.outputs = PurposeOutputs("alice", self.root / "outputs", self.sources, None, None, self.factory)

    def run_output(self, kind, identity=None):
        self.identity = identity or str(uuid4())
        self.outputs.start(self.identity, self.source, "说明本课处理流程", output_kind=kind, allow_model_text=True)
        self.outputs.wait(self.identity, 4)
        view = self.outputs.get(self.identity)
        if view["state"] == "interrupted":
            view["diagnostic_trace"] = self.outputs.typed.trace(self.identity)
        return view

    def tearDown(self):
        self.outputs.close()
        self.sources.close()
        self.temp.cleanup()

    def test_two_purposes_reuse_source_and_conflicting_uuid_is_rejected(self):
        for kind in ("source_answer", "method_list"):
            view = self.run_output(kind)
            self.assertEqual(view["state"], "available", (view, self.outputs.typed.trace(self.identity)))
            self.assertEqual(self.outputs.candidate(self.identity)["output_kind"], kind)
            self.outputs.start(self.identity, self.source, "说明本课处理流程", output_kind=kind, allow_model_text=True)
        self.assertEqual(self.calls, ["model", "model"])
        with self.assertRaises(NotesServiceError):
            self.outputs.start(self.identity, self.source, "说明本课处理流程", output_kind="source_answer", allow_model_text=True)

    def test_saved_image_analysis_requires_explicit_saved_media_and_available_frames(self):
        for reuse in (False, True):
            identity = str(uuid4())
            with self.assertRaises(NotesServiceError):
                self.outputs.start(identity, self.source, "区分界面和视频素材", output_kind="source_answer",
                    allow_model_text=True, use_saved_visuals=reuse, allow_model_images=True)
            self.assertFalse((self.outputs.bindings / (identity + ".json")).exists())
        self.assertEqual(self.calls, [])

    def test_unknown_model_is_not_replayed_on_duplicate_or_cold_read(self):
        self.fail_model = True
        view = self.run_output("source_answer")
        self.assertEqual(view["state"], "interrupted")
        identity = self.identity
        self.outputs.start(identity, self.source, "说明本课处理流程", output_kind="source_answer", allow_model_text=True)
        cold = PurposeOutputs("alice", self.root / "outputs", self.sources, None, None, self.factory)
        self.assertEqual(cold.get(identity)["state"], "interrupted")
        self.assertEqual(self.calls, ["model"])
        cold.close()

    def test_cancel_during_reply_and_changed_current_source_block_read(self):
        self.cancel = True
        self.assertEqual(self.run_output("source_answer")["state"], "canceled")
        self.cancel = False
        view = self.run_output("source_answer")
        self.assertEqual(view["state"], "available", view)
        scope_path = self.outputs.bindings / (self.identity + ".json")
        saved_scope = scope_path.read_bytes()
        changed_scope = json.loads(saved_scope)
        changed_scope["user_request"] = "另一个问题"
        scope_path.write_text(json.dumps(changed_scope), encoding="utf-8")
        try:
            with self.assertRaises(NotesServiceError):
                self.outputs.candidate(self.identity)
        finally:
            scope_path.write_bytes(saved_scope)
        candidate = self.sources.output_root / self.source / "candidate.json"
        raw = candidate.read_bytes()
        try:
            candidate.write_bytes(raw + b"changed")
            with self.assertRaises(NotesServiceError):
                self.outputs.candidate(self.identity)
        finally:
            candidate.write_bytes(raw)

    def test_blank_code_frames_are_not_eligible_figures(self):
        packet = json.loads(Path("D:/CodexWork/VideoActionAgent-visual-intake-20261015/runtime/vision/af565dfa-8c3b-5684-ab6c-5dd73fce936e/visual-material.json").read_bytes())
        self.assertEqual(eligible_frame_ids(packet["observation"]), {"f000"})


if __name__ == "__main__":
    unittest.main()
