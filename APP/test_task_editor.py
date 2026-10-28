"""Two offline content-editing paths; no model, I/O, or lifecycle integration."""
import copy
import importlib
import json
import sys
import unittest

sys.path.insert(0, "D:/CodexWork/VideoActionAgent-downstream-20261014")
from bridge_contract import BridgeError


class TaskEditorTests(unittest.TestCase):
    def fixture(self):
        request = "解释材料的主要论证和限定，再给可执行的建议；区分原观点和建议。"
        source_digest = "a" * 64
        material = {"source_evidence": {"bvid": "BVeditor", "page": 1, "cid": 6},
                    "transcription": {"segments": [
                        {"text": "只有把假设变成实际结果，才能检验是否有用。", "start": 0, "end": 4},
                        {"text": "发现不适用时，要根据反馈调整做法。", "start": 4, "end": 8}],
                        "runtime_metrics": {"local_only": "local-only-run"}}}
        solution = {"schema_version": 1, "user_request": request, "source_digest": source_digest,
                    "goal": "理解论证并选择可调整的做法", "success_criteria": ["说明检验条件", "区分建议与原观点"],
                    "proposed_deliverable": "解释及行动建议", "status": "ready", "gaps": [],
                    "markdown": "只要记录想法，就说明方法已经有效。", "tool_calls": []}
        return request, source_digest, material, solution

    def test_valid_source_edit_preserves_the_original_request_and_complete_solution(self):
        module = importlib.import_module("task_editor")
        request, source_digest, material, solution = self.fixture()
        original = copy.deepcopy((material, solution))
        prompt = module.build_editor_prompt(request, source_digest, material, solution)
        for segment in material["transcription"]["segments"]:
            self.assertIn(segment["text"], prompt)
        self.assertIn('"evidence_index":2', prompt)
        self.assertNotIn("local-only-run", prompt)
        self.assertNotIn(source_digest, prompt)
        frozen_policy = "当次冻结的内容编辑规约；不得改用重启后的默认策略。"
        frozen_prompt = module.build_editor_prompt(request, source_digest, material, solution, policy_text=frozen_policy)
        self.assertTrue(frozen_prompt.startswith(frozen_policy))
        self.assertNotIn(module.EDITOR_POLICY, frozen_prompt)
        for invalid_policy in ("", 7):
            with self.subTest(invalid_policy=invalid_policy), self.assertRaises(BridgeError):
                module.build_editor_prompt(request, source_digest, material, solution, policy_text=invalid_policy)
        markdown = "材料提出，假设需要通过实际结果检验；仅记录想法不足以说明方法有效。"
        response = {"schema_version": 1, "markdown": markdown, "changes": [{
            "kind": "source", "reason": "将记录想法与实际检验区分开，恢复来源的条件。",
            "evidence": [{"index": 1, "quote": "只有把假设变成实际结果，才能检验是否有用。"}]}]}
        raw = json.dumps(response, ensure_ascii=False)
        result = module.apply_editor(raw, request, source_digest, material, solution)
        self.assertEqual(result, {**solution, "markdown": markdown})
        for defect in ("forged_quote", "unknown_field"):
            malformed = copy.deepcopy(response)
            if defect == "forged_quote":
                malformed["changes"][0]["evidence"][0]["quote"] = "来源中没有出现过的断言。"
            else:
                malformed["source_digest"] = source_digest
            with self.subTest(defect=defect), self.assertRaises(BridgeError):
                module.apply_editor(json.dumps(malformed, ensure_ascii=False), request, source_digest, material, solution)
            self.assertEqual((material, solution), original)
        self.assertEqual((material, solution), original)

    def test_method_reorganization_and_verbatim_no_change_keep_existing_tools(self):
        module = importlib.import_module("task_editor")
        request, source_digest, material, solution = self.fixture()
        solution["markdown"] = ("主要论证及其条件需要完整说明，建议需要能回应具体困难。" * 110) + "\n[[TOOL:relation]]"
        solution["tool_calls"] = [{"id": "relation", "name": "render_diagram", "purpose": "解释检验与反馈的关系",
                                   "required": False, "arguments": {"title": "检验与反馈", "direction": "LR",
                                    "nodes": [{"id": "trial", "label": "实际检验", "evidence": [1]},
                                              {"id": "feedback", "label": "依据反馈调整", "evidence": [2]}],
                                    "edges": [{"source": "trial", "target": "feedback", "label": "", "evidence": [1, 2]}]}}]
        original = copy.deepcopy(solution)
        self.assertIsNotNone(module.editing_reason(request, material, solution))
        self.assertIsNone(module.editing_reason(request, material, {**solution, "markdown": "简短回答"}))
        markdown = ("材料的主要论证是用实际结果检验假设，并根据反馈调整。\n\n"
                    "建议：遇到无法判断是否适用的困难时，先尝试当前做法，观察实际结果；"
                    "若结果与预期不符，记录差异并据此调整。以上是建议，并非来源指定的计划。\n[[TOOL:relation]]")
        raw = json.dumps({"schema_version": 1, "markdown": markdown, "changes": [{
            "kind": "method", "reason": "保留主要论证，将重复说明改为困难、行动、结果和反馈的连接。", "evidence": []}]},
            ensure_ascii=False)
        result = module.apply_editor(raw, request, source_digest, material, solution)
        self.assertEqual(result, {**solution, "markdown": markdown})
        for marker in ("", "[[TOOL:renamed]]"):
            malformed = json.loads(raw)
            malformed["markdown"] = markdown.replace("[[TOOL:relation]]", marker)
            with self.subTest(replacement=marker), self.assertRaises(BridgeError):
                module.apply_editor(json.dumps(malformed, ensure_ascii=False), request, source_digest, material, solution)
            self.assertEqual(solution, original)
        unchanged = module.apply_editor(json.dumps({"schema_version": 1, "markdown": solution["markdown"], "changes": []}),
                                        request, source_digest, material, solution)
        self.assertEqual(unchanged, solution)
        self.assertIsNot(unchanged, solution)
        self.assertEqual(solution, original)


if __name__ == "__main__":
    unittest.main()
