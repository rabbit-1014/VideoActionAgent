"""A selected answer is context only when the owner explicitly continues it."""
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from purpose_app import build_app
from seed_sources import SOURCE_ID


class FollowupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.previous = Path.cwd()
        cls.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        cls.directory = Path(cls.temporary.name)

    @classmethod
    def tearDownClass(cls):
        from sqlalchemy.orm import close_all_sessions
        close_all_sessions()
        module = sys.modules.get('app.db.engine')
        if module:
            module.engine.dispose()
        logging.shutdown()
        os.chdir(cls.previous)
        cls.temporary.cleanup()

    def test_context_is_frozen_and_sent_across_two_followups_but_not_new_questions(self):
        calls = []
        class Provider:
            def complete(self, system, prompt):
                if 'parent_lines' in prompt:
                    data = json.loads(prompt.rsplit('\n', 1)[-1])
                    calls.append(data)
                    line = next(row for row in data['parent_lines'] if row['text'].strip())
                    return json.dumps({'edits': [{'line_id': line['line_id'],
                        'after': line['text']+'补充说明。'}]}, ensure_ascii=False)
                calls.append(json.loads(prompt))
                return json.dumps({'goal': '回答本次问题', 'success_criteria': ['回答所问'],
                    'proposed_deliverable': '短答', 'status': 'ready', 'gaps': [],
                    'markdown': f'第{len(calls)}份答复：保留已说明的条件。', 'tool_calls': []}, ensure_ascii=False)
        app = build_app(self.directory, provider_factory=Provider)
        with TestClient(app) as client:
            client.post('/api/bridge/session', json={'token': app.state.local_session})
            def submit(text, parent=None, identity=None):
                identity = identity or str(uuid4())
                body = {'request_id': identity, 'source_id': SOURCE_ID, 'user_request': text,
                    'output_kind': 'source_answer', 'allow_model_text': True}
                if parent:
                    body['followup'] = {'parent_job_id': parent['job_id'],
                        'expected_parent_fingerprint': parent['revision']['record_fingerprint']}
                response = client.post('/api/bridge/outputs', json=body)
                self.assertEqual(response.status_code, 200, response.json())
                app.state.outputs.wait(identity, 10)
                response = client.get('/api/bridge/outputs/'+identity)
                self.assertEqual(response.status_code, 200, response.json())
                return response.json(), body
            first, _ = submit('解释三图关系，不写学习计划')
            original = json.loads(json.dumps(first))
            second, body = submit('上面的关系为什么成立？我没有编程基础。', first)
            third, _ = submit('在这个前提下再简化，不要代码。', second)
            turns = app.state.outputs.binding(third['job_id'])['followup_context']['turns']
            self.assertEqual(third.get('conversation_turns'), turns)
            self.assertNotIn('conversation_turns', first)
            self.assertEqual([t['user_request'] for t in turns],
                ['解释三图关系，不写学习计划', '上面的关系为什么成立？我没有编程基础。'])
            self.assertEqual([t['markdown'] for t in turns], [first['result']['markdown'], second['result']['markdown']])
            sent = calls[2]['source_material_untrusted']['supplementary_context_untrusted']
            self.assertIn('我没有编程基础', sent)
            self.assertIn(first['result']['markdown'], sent)
            self.assertIn('不是事实来源', sent)
            self.assertEqual(calls[2]['user_request'], '在这个前提下再简化，不要代码。')
            self.assertEqual(len(calls[2]['source_material_untrusted']['transcription']['segments']), 111)
            self.assertEqual(client.post('/api/bridge/outputs', json=body).status_code, 200)
            self.assertEqual(len(calls), 3)
            self.assertEqual(client.post('/api/bridge/outputs', json={k: v for k, v in body.items()
                if k != 'followup'}).status_code, 409)
            self.assertEqual(len(calls), 3)
            self.assertEqual(client.get('/api/bridge/outputs/'+first['job_id']).json(), original)
            path = app.state.outputs.bindings/(third['job_id']+'.json')
            original_binding = path.read_bytes()
            try:
                changed = json.loads(original_binding)
                changed['followup_context']['turns'][0]['markdown'] = '被替换的历史'
                path.write_text(json.dumps(changed), encoding='utf-8')
                self.assertEqual(client.get('/api/bridge/outputs/'+third['job_id']).status_code, 409)
            finally:
                path.write_bytes(original_binding)
            self.assertEqual(client.get('/api/bridge/outputs/'+third['job_id']).status_code, 200)
            independent, _ = submit('只说本课主题')
            self.assertNotIn('followup_context', app.state.outputs.binding(independent['job_id']))
            self.assertNotIn('我没有编程基础', json.dumps(calls[3], ensure_ascii=False))
            self.assertFalse(independent['completed'])
            self.assertFalse(independent['quality_approved'])
            revised_id = str(uuid4())
            response = client.post('/api/bridge/revisions', json={'request_id': revised_id,
                'parent_job_id': third['job_id'],
                'expected_parent_fingerprint': third['revision']['record_fingerprint'],
                'instruction': '只补充一句说明，继续遵守前面的条件。', 'allow_model_text': True})
            self.assertEqual(response.status_code, 200, response.json())
            app.state.outputs.wait(revised_id, 10)
            response = client.get('/api/bridge/outputs/'+revised_id)
            self.assertEqual(response.status_code, 200, response.json())
            self.assertIn('我没有编程基础', calls[-1]['requested_material'].get('followup_dialogue_untrusted', ''))

    def test_wrong_parent_identity_or_fingerprint_is_rejected_before_model_or_binding(self):
        calls = []
        class Provider:
            def complete(self, system, prompt):
                calls.append(prompt)
                return json.dumps({'goal': '回答', 'success_criteria': ['所问'],
                    'proposed_deliverable': '短答', 'status': 'ready', 'gaps': [],
                    'markdown': '原稿内容。', 'tool_calls': []})
        app = build_app(self.directory, provider_factory=Provider)
        with TestClient(app) as client:
            client.post('/api/bridge/session', json={'token': app.state.local_session})
            parent_id = str(uuid4())
            body = {'request_id': parent_id, 'source_id': SOURCE_ID, 'user_request': '说明主题',
                    'output_kind': 'source_answer', 'allow_model_text': True}
            self.assertEqual(client.post('/api/bridge/outputs', json=body).status_code, 200)
            app.state.outputs.wait(parent_id, 10)
            parent = client.get('/api/bridge/outputs/'+parent_id).json()
            count = len(calls)
            from video_action_agent.source_candidate_bundle import save_bundle
            other_source = str(uuid4())
            bundle = app.state.sources.bundle(SOURCE_ID)
            save_bundle(bundle, app.state.sources.output_root/other_source)
            proof = bundle['source_envelope']['source_evidence']
            app.state.sources._store.save(other_source, {'job_id': other_source, 'state': 'available',
                'stage': None, 'bvid': proof['bvid'], 'page': proof['page'], 'error_code': None},
                {'bvid': proof['bvid'], 'page': proof['page'], 'allow_local_audio_read': True})
            identity = str(uuid4())
            result = client.post('/api/bridge/outputs', json={**body, 'request_id': identity,
                'source_id': other_source, 'followup': {'parent_job_id': parent_id,
                    'expected_parent_fingerprint': parent['revision']['record_fingerprint']}})
            self.assertEqual(result.status_code, 409)
            self.assertEqual(result.json()['admission'], 'rejected_before_create')
            self.assertFalse((app.state.outputs.bindings/(identity+'.json')).exists())
            for link in ({'parent_job_id': parent_id, 'expected_parent_fingerprint': '0'*64},
                         {'parent_job_id': str(uuid4()), 'expected_parent_fingerprint': parent['revision']['record_fingerprint']}):
                identity = str(uuid4())
                result = client.post('/api/bridge/outputs', json={**body, 'request_id': identity,
                    'user_request': '再解释一下', 'followup': link})
                self.assertEqual(result.status_code, 409)
                self.assertFalse((app.state.outputs.bindings/(identity+'.json')).exists())
            scope = app.state.outputs.bindings/(parent_id+'.json')
            original = scope.read_bytes()
            try:
                changed = json.loads(original)
                changed['owner'] = 'another-owner'
                scope.write_text(json.dumps(changed), encoding='utf-8')
                identity = str(uuid4())
                result = client.post('/api/bridge/outputs', json={**body, 'request_id': identity,
                    'followup': {'parent_job_id': parent_id,
                        'expected_parent_fingerprint': parent['revision']['record_fingerprint']}})
                self.assertEqual(result.status_code, 409)
                self.assertFalse((app.state.outputs.bindings/(identity+'.json')).exists())
            finally:
                scope.write_bytes(original)
            self.assertEqual(len(calls), count)

    def test_history_cannot_be_silently_truncated_or_rewritten(self):
        from followup_context import (
            MAX_CONTEXT_CHARS,
            material_context,
            snapshot,
            validate_context,
        )
        parent_id, child_id = str(uuid4()), str(uuid4())
        link = {'parent_job_id': parent_id, 'expected_parent_fingerprint': 'a'*64}
        with self.assertRaisesRegex(ValueError, 'followup_context_limit'):
            snapshot(link, {'request_id': parent_id, 'user_request': '已有要求'},
                {'result': {'markdown': '文'*MAX_CONTEXT_CHARS}}, child_id)
        value = snapshot(link, {'request_id': parent_id, 'user_request': '已有要求'},
            {'result': {'markdown': '旧稿不是权威来源。'}}, child_id)
        material = {'transcription': {'segments': [{'text': '来源原文'}]}, 'unknown_field': {'preserved': True}}
        projected = material_context(material, {'followup_context': value})
        self.assertEqual(projected['transcription'], material['transcription'])
        self.assertEqual(projected['unknown_field'], material['unknown_field'])
        self.assertNotIn('followup_dialogue_untrusted', material)
        value['turns'][0]['job_id'] = child_id
        with self.assertRaises(ValueError):
            validate_context(value, child_id)


if __name__ == '__main__':
    unittest.main()
