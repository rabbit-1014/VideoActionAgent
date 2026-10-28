"""Exercise default goal policy through the actual application admission and readback."""
import json
import logging
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from purpose_app import build_app
from seed_sources import SOURCE_ID

_TEST_DIRECTORY = None


@contextmanager
def local_runtime():
    previous = Path.cwd()
    try:
        yield Path(_TEST_DIRECTORY)
    finally:
        from sqlalchemy.orm import close_all_sessions
        close_all_sessions()
        engine_module = sys.modules.get('app.db.engine')
        if engine_module is not None:
            engine_module.engine.dispose()
        logging.shutdown()
        os.chdir(previous)


class GoalAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The upstream engine/output directory are process globals: reopen one
        # isolated runtime, matching the application's one-runtime-per-process.
        global _TEST_DIRECTORY
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        _TEST_DIRECTORY = temporary.name
        cls.addClassCleanup(temporary.cleanup)

    def test_addressed_revision_saves_one_local_change_and_keeps_parent(self):
        calls = []
        class Provider:
            def complete(self, system, prompt):
                calls.append('model')
                if 'parent_lines' in prompt:
                    context = json.loads(prompt.rsplit('\n', 1)[-1])
                    line = next(row for row in context['parent_lines'] if row['text'] == '- 老板 AI/CEO 助手')
                    return json.dumps({'edits': [{'line_id': line['line_id'],
                        'after': '- 老板 AI/CEO 助手：据讲者介绍，辅助识别信息失真，人负责决策。'}]}, ensure_ascii=False)
                return json.dumps({'goal': '理解材料', 'success_criteria': ['说明主题'],
                    'proposed_deliverable': '要点', 'status': 'ready', 'gaps': [],
                    'markdown': '# 原标题\n\n- 老板 AI/CEO 助手\n- 其他原文保持。',
                    'tool_calls': []}, ensure_ascii=False)
        with local_runtime() as directory:
            app = build_app(directory, provider_factory=Provider)
            with TestClient(app) as client:
                client.post('/api/bridge/session', json={'token': app.state.local_session})
                identity = str(uuid4())
                admitted = client.post('/api/bridge/outputs', json={'request_id': identity,
                    'source_id': SOURCE_ID, 'user_request': '整理主题，不补造代码',
                    'output_kind': 'structured_notes', 'allow_model_text': True})
                self.assertEqual(admitted.status_code, 200, admitted.json())
                app.state.outputs.wait(identity, 8)
                parent = client.get('/api/bridge/outputs/'+identity).json()
                child = str(uuid4())
                body = {'request_id': child, 'parent_job_id': identity,
                    'expected_parent_fingerprint': parent['revision']['record_fingerprint'],
                    'allow_model_text': True, 'instruction': '只补老板AI的作用和责任边界'}
                admitted = client.post('/api/bridge/revisions', json=body)
                self.assertEqual(admitted.status_code, 200, admitted.json())
                app.state.outputs.wait(child, 8)
                response = client.get('/api/bridge/outputs/'+child)
                self.assertEqual(response.status_code, 200, response.json())
                self.assertEqual(response.json()['result']['markdown'],
                    '# 原标题\n\n- 老板 AI/CEO 助手：据讲者介绍，辅助识别信息失真，人负责决策。\n- 其他原文保持。')
                self.assertEqual(calls, ['model', 'model'])
                self.assertEqual(response.json()['model_calls'], 1)
                self.assertEqual(app.state.outputs.binding(child)['revision_strategy'], 'line_text_edits_v1')
                self.assertEqual(client.get('/api/bridge/outputs/'+identity).json(), parent)
                self.assertEqual(client.post('/api/bridge/revisions', json=body).status_code, 200)
                self.assertEqual(calls, ['model', 'model'])
                proof = json.loads((directory/'reused-visual'/child/'amendment.json').read_bytes())
                self.assertFalse(proof['semantic_quality_approved'])
                proof_path = directory/'reused-visual'/child/'amendment.json'
                original_proof = proof_path.read_bytes()
                try:
                    proof_path.write_text('{}', encoding='utf-8')
                    self.assertEqual(client.get('/api/bridge/outputs/'+child).status_code, 409)
                finally:
                    proof_path.write_bytes(original_proof)
                self.assertEqual(client.get('/api/bridge/outputs/'+child).status_code, 200)

    def test_leaf_source_patch_is_bound_applied_and_saved_without_graph_replacement(self):
        calls = []
        class Provider:
            def complete(self, system, prompt):
                if 'draft_solution_untrusted' in prompt:
                    calls.append('review')
                    context = json.loads(prompt.rsplit('\n', 1)[-1])
                    quote = context['source_envelope_untrusted']['transcription']['segments'][0]['text'][:30]
                    return json.dumps({'edits': [], 'tool_edits': [{'call_id': 'diagram1',
                        'path': '/edges/0/label', 'before': 'UNSUPPORTED_CODE', 'after': '课程关联',
                        'reason': '去除未证代码式拼写，保留原图结构。',
                        'evidence': [{'index': 1, 'quote': quote}]}], 'resolution': None}, ensure_ascii=False)
                calls.append('generate')
                return json.dumps({'goal': '解释课程关系', 'success_criteria': ['保留课程关联'],
                    'proposed_deliverable': '简短解释', 'status': 'ready', 'gaps': [],
                    'markdown': '课程关系如下。\n\n[[TOOL:diagram1]]',
                    'tool_calls': [{'id': 'diagram1', 'name': 'render_diagram', 'purpose': '表示局部关系',
                        'required': False, 'arguments': {'title': '课程关系', 'direction': 'LR',
                            'nodes': [{'id': 'a', 'label': '文本', 'evidence': [1]},
                                      {'id': 'b', 'label': '课程', 'evidence': [1]}],
                            'edges': [{'source': 'a', 'target': 'b', 'label': 'UNSUPPORTED_CODE',
                                       'evidence': [1]}]}}]}, ensure_ascii=False)
        with local_runtime() as directory:
            app = build_app(directory, provider_factory=Provider)
            with TestClient(app) as client:
                client.post('/api/bridge/session', json={'token': app.state.local_session})
                identity = str(uuid4())
                admitted = client.post('/api/bridge/outputs', json={'request_id': identity,
                    'source_id': SOURCE_ID, 'user_request': '解释材料关系，不补造代码',
                    'output_kind': 'source_answer', 'allow_model_text': True})
                self.assertEqual(admitted.status_code, 200, admitted.json())
                app.state.outputs.wait(identity, 10)
                response = client.get('/api/bridge/outputs/'+identity)
                self.assertEqual(response.status_code, 200, response.json())
                self.assertEqual(calls, ['generate', 'review'])
                result = response.json()
                self.assertEqual(result['model_calls'], 2)
                self.assertFalse(result['quality_approved'])
                manifest = json.loads((directory/'goal-results'/identity/'manifest.json').read_bytes())
                args = manifest['solution']['tool_calls'][0]['arguments']
                self.assertEqual(args['edges'][0], {'source': 'a', 'target': 'b',
                    'label': '课程关联', 'evidence': [1]})
                self.assertEqual(len(args['nodes']), 2)
                self.assertTrue((directory/'goal-results'/identity/'source-review-conversion.json').exists())
                self.assertEqual(app.state.outputs.native.ledger.get(identity)['source_review_stage'], 'source_review_v2')

    def test_new_wire_uses_existing_native_profile_and_reads_bound_content(self):
        calls = []
        class Provider:
            def complete(self, system, prompt):
                calls.append('model')
                if 'current_solution_untrusted' in prompt:
                    return json.dumps({'schema_version': 1, 'markdown': '先明确目标，再根据实际结果调整方法。',
                        'changes': [{'kind': 'method', 'reason': '合并重复内容，将行动与结果联系。', 'evidence': []}]}, ensure_ascii=False)
                payload = json.loads(prompt)
                markdown = ('先说明目标与结果的关系，再判断做法是否适用。' * 140
                            if '详细解释' in payload['user_request'] else '本课讲解 Markdown 文件分块与知识图谱构建。')
                value = {'goal': '回答课程主题', 'success_criteria': ['简短说明主题'],
                    'proposed_deliverable': '一句话', 'status': 'ready', 'gaps': [],
                    'markdown': markdown, 'tool_calls': []}
                if 'schema_version' in payload['response_schema']:
                    value['schema_version'] = payload['response_schema']['schema_version']
                return json.dumps(value, ensure_ascii=False)
        with local_runtime() as directory:
            app = build_app(Path(directory), provider_factory=Provider)
            with TestClient(app) as client:
                client.post('/api/bridge/session', json={'token': app.state.local_session})
                identity = str(uuid4())
                response = client.post('/api/bridge/outputs', json={'request_id': identity,
                    'source_id': SOURCE_ID, 'user_request': '一句话说明本课主题，不加图',
                    'output_kind': 'source_answer', 'allow_model_text': True})
                self.assertEqual(response.status_code, 200, response.json())
                app.state.outputs.wait(identity, 8)
                result = client.get('/api/bridge/outputs/'+identity)
                self.assertEqual(result.status_code, 200, result.json())
                self.assertIn('Markdown', result.json()['result']['markdown'])
                binding = app.state.outputs.binding(identity)
                self.assertEqual(binding['native_profile']['version'], 6)
                self.assertEqual(binding['goal_policy']['version'], 14)
                request_record = json.loads((directory/'native-engine/notes/native'/(identity+'.model-request.json')).read_bytes())
                self.assertEqual(request_record['model'], 'deepseek-v4-pro')
                self.assertFalse(request_record['stream'])
                self.assertEqual(result.json()['generation_model'], 'deepseek-v4-pro')
                self.assertEqual(calls, ['model'])
                edited_id = str(uuid4())
                response = client.post('/api/bridge/outputs', json={'request_id': edited_id,
                    'source_id': SOURCE_ID, 'user_request': '详细解释材料关系并提出有据的建议，不加图',
                    'output_kind': 'structured_notes', 'allow_model_text': True})
                self.assertEqual(response.status_code, 200, response.json())
                app.state.outputs.wait(edited_id, 8)
                edited = client.get('/api/bridge/outputs/'+edited_id)
                self.assertEqual(edited.status_code, 200, edited.json())
                self.assertIn('先说明目标与结果的关系', edited.json()['result']['markdown'])
                self.assertEqual(edited.json()['model_calls'], 1)
                ledger_row = app.state.outputs.native.ledger.get(edited_id)
                self.assertNotIn('task_editing_route', ledger_row)
                self.assertEqual(calls, ['model', 'model'])
                # A damaged, unrelated initial admission must not prevent an
                # otherwise valid family from using the existing local editor.
                from bridge_contract import atomic
                unrelated_id = str(uuid4())
                unrelated = json.loads(json.dumps(app.state.outputs.binding(identity)))
                unrelated['request_id'] = unrelated_id
                unrelated['native_profile']['version'] = 99
                atomic(app.state.outputs.bindings/(unrelated_id+'.json'), unrelated)
                local_id = str(uuid4())
                changed = client.post('/api/bridge/revisions', json={'request_id': local_id,
                    'parent_job_id': identity,
                    'expected_parent_fingerprint': result.json()['revision']['record_fingerprint'],
                    'instruction': '只补充标题说明', 'allow_model_text': False, 'allow_local_edits': True,
                    'exact_edits': [{'before': '本课讲解 Markdown 文件分块与知识图谱构建。',
                                     'after': '本课讲解 Markdown 文件分块与知识图谱构建。这里是本地修订。'}]})
                self.assertEqual(changed.status_code, 200, changed.json())
                app.state.outputs.wait(local_id, 8)
                local = client.get('/api/bridge/outputs/'+local_id)
                self.assertEqual(local.status_code, 200, local.json())
                self.assertIn('这里是本地修订', local.json()['result']['markdown'])
                self.assertEqual(local.json()['model_calls'], 0)
                self.assertEqual(calls, ['model', 'model'])
                related_id = str(uuid4())
                unrelated['request_id'] = related_id
                unrelated['native_lineage'] = {'root_job_id': identity, 'version': 3, 'parent_job_id': local_id}
                atomic(app.state.outputs.bindings/(related_id+'.json'), unrelated)
                refused_id = str(uuid4())
                refused = client.post('/api/bridge/revisions', json={'request_id': refused_id,
                    'parent_job_id': local_id,
                    'expected_parent_fingerprint': local.json()['revision']['record_fingerprint'],
                    'instruction': '不能跨过本家族坏记录', 'allow_model_text': False, 'allow_local_edits': True,
                    'exact_edits': [{'before': '这里是本地修订', 'after': '第二次修订'}]})
                self.assertEqual(refused.status_code, 409)
                self.assertFalse((app.state.outputs.bindings/(refused_id+'.json')).exists())
                self.assertEqual(calls, ['model', 'model'])
                # Missing lineage is not proof of independence when a revision
                # request still points into this family.
                unrelated.pop('native_lineage')
                unrelated['native_profile'] = app.state.outputs.binding(identity)['native_profile']
                unrelated['revision_request'] = {'parent_job_id': local_id,
                    'expected_parent_fingerprint': local.json()['revision']['record_fingerprint'],
                    'instruction': '损坏的修订记录'}
                unrelated['revision_strategy'] = 'exact_text_edits_v1'
                atomic(app.state.outputs.bindings/(related_id+'.json'), unrelated)
                refused_id2 = str(uuid4())
                body = json.loads(refused.request.content)
                body['request_id'] = refused_id2
                self.assertEqual(client.post('/api/bridge/revisions', json=body).status_code, 409)
                self.assertFalse((app.state.outputs.bindings/(refused_id2+'.json')).exists())
                unrelated.pop('revision_request')
                unrelated.pop('revision_strategy')
                unrelated['native_lineage'] = {}
                atomic(app.state.outputs.bindings/(related_id+'.json'), unrelated)
                refused_id3 = str(uuid4())
                body['request_id'] = refused_id3
                self.assertEqual(client.post('/api/bridge/revisions', json=body).status_code, 409)
                self.assertFalse((app.state.outputs.bindings/(refused_id3+'.json')).exists())


if __name__ == '__main__':
    unittest.main()
