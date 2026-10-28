"""Actual application save lifecycle; no external model, browser or source read."""
import copy
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from test_explanation_integration import arguments
from purpose_app import build_app
from seed_sources import SOURCE_ID


class ExplanationLifecycleTests(unittest.TestCase):
    def test_plain_to_visual_update_remove_preserves_source_and_previous_versions(self):
        calls = []
        class Provider:
            def complete(self, system, prompt):
                calls.append(prompt)
                if len(calls) == 1:
                    return json.dumps({'goal': '理解关系', 'success_criteria': ['保留对象区别'],
                        'proposed_deliverable': '简短解释', 'status': 'ready', 'gaps': [], 'tool_calls': [],
                        'markdown': '# 内容\n\n## 关系说明\n\n文字解释。\n\n末尾原文保持。'}, ensure_ascii=False)
                context = json.loads(prompt.rsplit('\n', 1)[-1])
                if len(calls) == 2:
                    line = next(r for r in context['parent_lines'] if r['text'] == '## 关系说明')
                    payload = {'edits': [], 'new_explanations': [
                        {'after_line_id': line['line_id'], 'arguments': arguments()}]}
                elif len(calls) == 3:
                    args = copy.deepcopy(arguments())
                    args['panels'][0]['explanation'] = '这次更新说明，保留原文结构。'
                    payload = {'edits': [], 'explanation_updates': [{
                        'call_id': context['bound_explanations'][0]['call_id'], 'arguments': args}]}
                else:
                    payload = {'edits': [], 'explanation_removals': [context['bound_explanations'][0]['call_id']]}
                return json.dumps(payload, ensure_ascii=False)
        previous = Path.cwd()
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        app = None
        try:
            app = build_app(Path(temporary.name), provider_factory=Provider, generation_provider='codex')
            with TestClient(app) as client:
                client.post('/api/bridge/session', json={'token': app.state.local_session})
                first = str(uuid4())
                response = client.post('/api/bridge/outputs', json={'request_id': first, 'source_id': SOURCE_ID,
                    'user_request': '说明关系', 'output_kind': 'source_answer', 'allow_model_text': True})
                self.assertEqual(response.status_code, 200, response.text)
                app.state.outputs.wait(first, 10)
                first_response = client.get('/api/bridge/outputs/' + first)
                self.assertEqual(first_response.status_code, 200, first_response.text)
                parent = first_response.json()
                versions = [parent]
                self.assertEqual(parent.get('explanations'), [])
                for instruction in ('在关系说明后插入示意图', '更新第一面板的说明', '删除这张图，保留其他原文'):
                    identity = str(uuid4())
                    response = client.post('/api/bridge/revisions', json={'request_id': identity,
                        'parent_job_id': parent['job_id'],
                        'expected_parent_fingerprint': parent['revision']['record_fingerprint'],
                        'instruction': instruction, 'allow_model_text': True})
                    self.assertEqual(response.status_code, 200, response.text)
                    app.state.outputs.wait(identity, 10)
                    response = client.get('/api/bridge/outputs/' + identity)
                    if response.status_code != 200:
                        state = app.state.outputs.get(identity)
                        self.fail(json.dumps({key: state.get(key) for key in
                            ('state', 'error_code', 'model_calls')}, ensure_ascii=False))
                    self.assertEqual(response.status_code, 200, response.text)
                    parent = response.json()
                    self.assertEqual(parent['source_id'], versions[0]['source_id'])
                    self.assertIn('末尾原文保持。', parent['result']['markdown'])
                    for old in versions:
                        self.assertEqual(client.get('/api/bridge/outputs/' + old['job_id']).json(), old)
                    versions.append(parent)
                self.assertEqual(len(versions[1]['explanations']), 1)
                self.assertIn('added_explanation_', versions[1]['explanations'][0]['call_id'])
                self.assertEqual(versions[2]['explanations'][0]['panels'][0]['explanation'],
                                 '这次更新说明，保留原文结构。')
                self.assertEqual(versions[3]['explanations'], [])
                self.assertNotIn('goal_explanation_', versions[3]['result']['markdown'])
                download = client.get('/api/bridge/notes/' + parent['job_id'] + '/download')
                self.assertEqual(download.content.decode(), parent['result']['markdown'])
                self.assertEqual(len(calls), 4)
        finally:
            if app:
                app.state.outputs.close()
            from sqlalchemy.orm import close_all_sessions
            close_all_sessions()
            module = sys.modules.get('app.db.engine')
            if module:
                module.engine.dispose()
            logging.shutdown()
            os.chdir(previous)
            temporary.cleanup()


if __name__ == '__main__':
    unittest.main()
