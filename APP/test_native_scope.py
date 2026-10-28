"""Purpose binding fence around the existing native notes service."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from uuid import uuid4

sys.path[:0] = [
    "C:/Users/泽/Documents/VideoActionAgent/src",
    "D:/CodexWork/VideoActionAgent-downstream-20261014",
    "D:/CodexWork/VideoActionAgent-visual-intake-20261015",
]
from bridge_contract import BridgeError
from video_action_agent.notes_service import NotesServiceError
from video_action_agent.source_candidate_bundle import read_bundle, save_bundle
from video_action_agent.source_capture import SourceCapture

from native_scope import ScopedNativeNotes


class NativePurposeScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        bundle = read_bundle(Path(
            "D:/CodexWork/VideoActionAgent-visual-intake-20261015/runtime/sources/"
            "8db58bff-b71b-5821-b129-b273bb2d7a68"
        ))
        self.source_id = str(uuid4())
        def acquire(source, output, **kwargs):
            save_bundle(bundle, output)
        self.sources = SourceCapture("alice", self.root / "sources", capture_fn=acquire)
        self.sources.start(self.source_id,
            "https://www.bilibili.com/video/BV1AJen6SEVQ?p=42", allow_local_audio_read=True)
        self.sources.wait(self.source_id, 3)
        self.binding = {
            "output_kind": "structured_notes", "request": "整理本课",
            "native_profile": {"version": 1, "style": "minimal",
                "instructions": "轻量整理", "sha256": "frozen-profile-hash"},
        }
        self.calls = []

    def service(self, generate, transport):
        return ScopedNativeNotes(
            "alice", self.root / "notes", self.sources, generate, transport,
            scope_value=lambda identity: dict(self.binding),
        )

    def generate(self, identity, material, request, style, client, output):
        self.calls.append("generate")
        text = client.chat.completions.create(
            model="glm-5.3", messages=[{"role": "user", "content": request}]
        ).choices[0].message.content
        target = output / (identity + ".json")
        target.write_text(json.dumps({"markdown": text,
            "transcript": material["transcription"], "audio_meta": {}}), encoding="utf-8")
        return target

    def test_generation_duplicate_and_cold_read_are_bound_to_frozen_purpose(self):
        def transport(prompt):
            self.calls.append("transport")
            return "# 本课笔记"
        notes = self.service(self.generate, transport)
        identity = str(uuid4())
        notes.start(identity, self.source_id, "整理本课", allow_model_text=True)
        notes.wait(identity, 4)
        self.assertEqual(notes.get(identity)["state"], "available")
        self.assertEqual(notes.ledger.get(identity)["style"], "minimal")
        self.assertEqual(notes.candidate(identity)["result"]["markdown"], "# 本课笔记")
        with self.assertRaises(BridgeError):
            notes.ledger.admit(identity, self.source_id, "整理本课",
                allow_model_text=True, style="tutorial")
        notes.start(identity, self.source_id, "整理本课", allow_model_text=True)
        self.assertEqual(self.calls, ["generate", "transport"])
        notes.close()

        self.binding["output_kind"] = "source_answer"
        cold = self.service(self.generate, transport)
        with self.assertRaises(NotesServiceError):
            cold.candidate(identity)
        with self.assertRaises(NotesServiceError):
            cold.get(identity)
        cold.close()

    def test_scope_change_before_native_client_boundary_prevents_transport(self):
        at_native_boundary = threading.Event()
        allow_native_boundary = threading.Event()

        def generate(identity, material, request, style, client, output):
            self.calls.append("generate")
            at_native_boundary.set()
            if not allow_native_boundary.wait(5):
                raise AssertionError("test did not release native boundary")
            client.chat.completions.create(
                model="glm-5.3", messages=[{"role": "user", "content": request}]
            )
            raise AssertionError("scope guard should stop before client transport")

        def transport(prompt):
            self.calls.append("transport")
            return "不应到达"

        notes = self.service(generate, transport)
        identity = str(uuid4())
        try:
            notes.start(identity, self.source_id, "整理本课", allow_model_text=True)
            self.assertTrue(at_native_boundary.wait(3))
            self.binding["request"] = "另一个请求"
            allow_native_boundary.set()
            notes.wait(identity, 4)
            row = notes.ledger.get(identity)
            self.assertEqual(row["state"], "native_failed_or_unconfirmed")
            self.assertEqual(row["error_code"], "purpose_scope_changed")
            self.assertEqual(row["model_calls"], 0)
            self.assertEqual(self.calls, ["generate"])
        finally:
            allow_native_boundary.set()
            thread = notes._threads.get(identity)
            if thread is not None and thread.ident is not None:
                thread.join()
            notes.close()

    def test_current_policy_can_admit_only_one_source_review(self):
        for version in (14, 15):
            with self.subTest(version=version):
                self.binding.update(goal_policy={"version": version}, allow_model_text=True)
                notes = self.service(self.generate, lambda prompt: "unused")
                identity = str(uuid4())
                try:
                    notes.ledger.admit(identity, self.source_id, "整理本课", allow_model_text=True)
                    notes.ledger.dispatch(identity)
                    notes.ledger.update(identity, state="reply_received")
                    row = notes.ledger.dispatch_review(identity)
                    self.assertEqual(row["model_calls"], 2)
                    self.assertTrue(row["source_review_dispatched"])
                    with self.assertRaisesRegex(BridgeError, "source_review_not_admitted"):
                        notes.ledger.dispatch_review(identity)
                    self.assertEqual(notes.ledger.get(identity)["model_calls"], 2)
                    self.assertEqual(self.calls, [])
                finally:
                    notes.close()

    def tearDown(self):
        self.sources.close()
        self.temp.cleanup()


if __name__ == "__main__":
    unittest.main()
