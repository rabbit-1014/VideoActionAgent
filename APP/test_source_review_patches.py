"""Offline checks for the source-review v2 string-leaf patch adapter."""
import copy
import importlib
import json
import sys
import unittest

sys.path.insert(0, "D:/CodexWork/VideoActionAgent-downstream-20261014")
import source_review as legacy
from bridge_contract import BridgeError, digest


class SourceReviewPatchTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec("source_review_patches"),
                             "source-review leaf patches have not been implemented")
        return importlib.import_module("source_review_patches")

    def fixture(self):
        request, source = "Explain the supplied relation without inventing identifiers.", "a" * 64
        material = {"transcription": {"segments": [
            {"text": "The two objects are linked with each other."},
            {"text": "The first object is the input and the second is the output."}]}}
        args = {"title": "Relation", "direction": "LR", "nodes": [
            {"id": "a", "label": "First", "evidence": [1]},
            {"id": "b", "label": "Second", "evidence": [2]}],
            "edges": [{"source": "a", "target": "b", "label": "LINKS_WITH", "evidence": [1]}]}
        solution = {"schema_version": 1, "user_request": request, "source_digest": source,
            "goal": "Explain the supplied relation.", "success_criteria": ["Keep the source terms."],
            "proposed_deliverable": "A short explanation.", "status": "ready", "gaps": [],
            "markdown": "The relation is shown below.\n\n[[TOOL:diagram1]]",
            "tool_calls": [{"id": "diagram1", "name": "render_diagram", "purpose": "Show the relation.",
                            "required": False, "arguments": args}]}
        evidence = [{"index": 1, "quote": "linked with"}]
        patch = {"call_id": "diagram1", "path": "/edges/0/label", "before": "LINKS_WITH",
                 "after": "linked with", "reason": "Use the wording actually supplied.", "evidence": evidence}
        payload = {"edits": [], "tool_edits": [patch], "resolution": None}
        context = {"request_id": "request-local", "request": request, "source_digest": source,
                   "material": material, "solution": solution}
        return context, payload

    def test_exact_leaf_coalesces_and_keeps_the_legacy_full_arguments_contract(self):
        module = self.module()
        context, payload = self.fixture()
        original = copy.deepcopy(context)
        args = context["solution"]["tool_calls"][0]["arguments"]
        old_patch = {key: copy.deepcopy(value) for key, value in payload["tool_edits"][0].items() if key != "path"}
        old_patch.update(before={"edges": copy.deepcopy(args["edges"])},
                         after={"edges": [{**args["edges"][0], "label": "linked with"}]})
        old_payload = {"schema_version": 1, "source_digest": context["source_digest"],
                       "draft_sha256": digest(context["solution"]), "edits": [],
                       "tool_edits": [old_patch], "resolution": None}
        # This is the decisive shape contradiction observed in T2F: only edges
        # cannot satisfy v1's exact full-arguments before contract.
        with self.assertRaisesRegex(BridgeError, "source_review_tool_before_mismatch"):
            legacy.apply_review(json.dumps(old_payload), **context)
        full_payload = copy.deepcopy(old_payload)
        full_payload["tool_edits"][0].update(before=copy.deepcopy(args), after=copy.deepcopy(args))
        full_payload["tool_edits"][0]["after"]["edges"][0]["label"] = "linked with"
        old_result = legacy.apply_review(json.dumps(full_payload), **context)

        payload["tool_edits"].append({"call_id": "diagram1", "path": "/nodes/0/label", "before": "First",
            "after": "Input", "reason": "Keep the stated role.",
            "evidence": [{"index": 2, "quote": "first object is the input"}]})
        raw = json.dumps(payload)
        converted = module.convert_review(raw, **context)
        self.assertEqual(converted["legacy_payload"]["tool_edits"][0]["before"], args)
        self.assertEqual(len(converted["legacy_payload"]["tool_edits"]), 1)
        self.assertEqual(converted["metadata"]["original_tool_patches"], payload["tool_edits"])
        self.assertEqual(converted["metadata"]["draft_sha256"], digest(context["solution"]))
        self.assertFalse(converted["metadata"]["quality_approved"])
        result = module.apply_review(raw, **context)
        expected = copy.deepcopy(old_result)
        expected["tool_calls"][0]["arguments"]["nodes"][0]["label"] = "Input"
        self.assertEqual(result, expected)
        self.assertEqual(context, original)
        self.assertEqual(json.loads(raw), payload)

    def test_bad_paths_identity_evidence_and_conflicts_do_not_change_input(self):
        module = self.module()
        for defect in ("id", "endpoint", "evidence", "title", "direction", "structure", "missing", "index_alias",
                       "bad_escape", "cross_path", "cross_call", "before", "quote", "duplicate", "delete_conflict",
                       "identity_echo", "version_echo", "duplicate_key"):
            with self.subTest(defect=defect):
                context, payload = self.fixture()
                original = copy.deepcopy(context)
                patch = payload["tool_edits"][0]
                forbidden = {"id": "/nodes/0/id", "endpoint": "/edges/0/source",
                    "evidence": "/edges/0/evidence/0", "title": "/title", "direction": "/direction", "structure": "/edges",
                    "missing": "/nodes/2/label", "index_alias": "/nodes/00/label", "bad_escape": "/nodes/~2/label",
                    "cross_path": "/tool_calls/1/arguments/edges/0/label"}
                if defect in forbidden:
                    patch["path"] = forbidden[defect]
                elif defect == "cross_call":
                    patch["call_id"] = "diagram2"
                elif defect == "before":
                    patch["before"] = "WRONG"
                elif defect == "quote":
                    patch["evidence"] = [{"index": 2, "quote": "linked with"}]
                elif defect == "duplicate":
                    payload["tool_edits"].append(copy.deepcopy(patch))
                elif defect == "delete_conflict":
                    payload["tool_edits"].append({**patch, "path": None, "before": None, "after": None})
                elif defect == "identity_echo":
                    payload["source_digest"] = context["source_digest"]
                elif defect == "version_echo":
                    payload["schema_version"] = 2
                raw = json.dumps(payload)
                if defect == "duplicate_key":
                    raw = raw[:-1] + ',"resolution":null}'
                with self.assertRaises(BridgeError):
                    module.apply_review(raw, **context)
                self.assertEqual(context, original)

    def test_optional_delete_requires_marker_and_preserves_required_tools(self):
        module = self.module()
        for mode in ("delete", "marker_remains", "required", "not_explicit"):
            with self.subTest(mode=mode):
                context, payload = self.fixture()
                payload["tool_edits"][0].update(path=None, before=None, after=None)
                payload["edits"] = [{"before": "[[TOOL:diagram1]]", "after": "", "reason": "Remove the optional diagram.",
                                     "evidence": [{"index": 1, "quote": "linked with"}]}]
                if mode == "marker_remains":
                    payload["edits"] = []
                elif mode == "required":
                    context["solution"]["tool_calls"][0]["required"] = True
                elif mode == "not_explicit":
                    payload["tool_edits"][0]["before"] = "LINKS_WITH"
                original = copy.deepcopy(context)
                if mode == "delete":
                    result = module.apply_review(json.dumps(payload), **context)
                    self.assertEqual(result["tool_calls"], [])
                    self.assertNotIn("[[TOOL:", result["markdown"])
                else:
                    with self.assertRaises((BridgeError, ValueError)):
                        module.apply_review(json.dumps(payload), **context)
                self.assertEqual(context, original)

    def test_prompt_uses_frozen_v2_policy_and_noop_or_resolution_keeps_canonical_shape(self):
        module = self.module()
        context, _ = self.fixture()
        prompt = module.build_prompt(context["request"], context["source_digest"], context["material"], context["solution"])
        self.assertTrue(prompt.startswith(module.PATCH_POLICY))
        self.assertIn("RFC6901", module.PATCH_POLICY)
        self.assertIn("/edges/0/label", module.PATCH_POLICY)
        self.assertIn('"source_envelope_untrusted"', prompt)
        self.assertNotIn("原样回显", prompt)
        frozen = "Frozen v2 policy owned by the caller."
        self.assertTrue(module.build_prompt(context["request"], context["source_digest"], context["material"],
                        context["solution"], policy_text=frozen).startswith(frozen))
        for invalid in ("", " ", False):
            with self.subTest(policy=invalid), self.assertRaises(BridgeError):
                module.build_prompt(context["request"], context["source_digest"], context["material"],
                                    context["solution"], policy_text=invalid)
        for resolution in (None, {"status": "partial", "gaps": [{"kind": "material", "required": False,
            "description": "Only the supplied relation is available."}], "reason": "Keep the limited scope visible.",
            "evidence": [{"index": 1, "quote": "linked with"}]}):
            payload = {"edits": [], "tool_edits": [], "resolution": resolution}
            result = module.apply_review(json.dumps(payload), **context)
            expected = copy.deepcopy(context["solution"])
            if resolution:
                expected.update(status=resolution["status"], gaps=resolution["gaps"])
            self.assertEqual(result, expected)

    def test_source_quotes_cannot_clear_required_execution_or_user_decision_gaps(self):
        module = self.module()
        for kind in ('capability', 'clarification'):
            with self.subTest(kind=kind):
                context, _ = self.fixture()
                gap = {'kind': kind, 'required': True,
                       'description': 'Independent checking is unavailable.' if kind == 'capability'
                                      else 'The recipient must choose the requested destination.'}
                context['solution'].update(status='partial', gaps=[gap])
                original = copy.deepcopy(context)
                resolution = {'status': 'ready', 'gaps': [], 'reason': 'The source explains the relation.',
                              'evidence': [{'index': 1, 'quote': 'linked with'}]}
                for gaps in ([], [{'kind': kind, 'required': False, 'description': gap['description']}]):
                    resolution['gaps'] = gaps
                    payload = {'edits': [], 'tool_edits': [], 'resolution': resolution}
                    with self.assertRaisesRegex(BridgeError, 'source_review_required_gap_changed'):
                        module.apply_review(json.dumps(payload), **context)
                self.assertEqual(context, original)

    def test_source_review_preserves_required_gap_but_can_resolve_material_with_existing_evidence(self):
        module = self.module()
        context, _ = self.fixture()
        gap = {'kind': 'capability', 'required': True, 'description': 'Independent checking is unavailable.'}
        context['solution'].update(status='partial', gaps=[gap])
        payload = {'edits': [], 'tool_edits': [], 'resolution': {'status': 'partial', 'gaps': [gap],
            'reason': 'Source review does not supply independent checking.',
            'evidence': [{'index': 1, 'quote': 'linked with'}]}}
        self.assertEqual(module.apply_review(json.dumps(payload), **context), context['solution'])
        payload['resolution']['gaps'] = [{**gap, 'description': 'No independent retrieval tool is available.'}]
        revised = module.apply_review(json.dumps(payload), **context)
        self.assertEqual(revised['status'], 'partial')
        self.assertEqual(revised['gaps'][0]['description'], 'No independent retrieval tool is available.')
        context['solution']['gaps'].append({**gap, 'description': 'The requested file executor is unavailable.'})
        with self.assertRaisesRegex(BridgeError, 'source_review_required_gap_changed'):
            module.apply_review(json.dumps(payload), **context)
        context['solution']['gaps'] = [{'kind': 'material', 'required': True,
                                      'description': 'The relation has not been located.'}]
        payload['resolution'].update(status='ready', gaps=[], reason='The relation appears in the supplied passage.')
        result = module.apply_review(json.dumps(payload), **context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['gaps'], [])
        self.assertEqual(result['markdown'], context['solution']['markdown'])


if __name__ == "__main__":
    unittest.main()
