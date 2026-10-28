"""One isolated real application lifecycle with deterministic provider responses."""
import copy
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_explanation_integration import arguments
from purpose_app import build_app
from seed_sources import SOURCE_ID


class ExplanationAppTests(unittest.TestCase):
    def test_rich_generation_revision_read_and_download_keep_old_result(self):
        calls = []
        class Provider:
            def complete(self, system, prompt):
                calls.append(prompt)
                if 'bound_explanations' in prompt:
                    args = copy.deepcopy(arguments())
                    args['panels'][0]['explanation'] = '更新说明，原文仍保留。'
                    return json.dumps({'edits': [], 'explanation_updates': [
                        {'call_id': 'representation', 'arguments': args}]}, ensure_ascii=False)
                return json.dumps({'goal': '理解关系', 'success_criteria': ['保留区别'],
                    'proposed_deliverable': '关系说明', 'status': 'ready', 'gaps': [],
                    'markdown': '# 内容\n\n[[TOOL:representation]]\n\n两者来源相同，对象不同。',
                    'tool_calls': [{'id': 'representation', 'name': 'render_explanation',
                        'purpose': '同对象对照', 'required': False, 'arguments': arguments()}]}, ensure_ascii=False)
        previous = Path.cwd()
        app = None
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        try:
            app = build_app(Path(temporary.name), generation_provider='codex')
            with patch('codex_exec_provider.CodexExecProvider', side_effect=lambda **_: Provider()) as codex, \
                    patch('purpose_app.ZhipuProvider', side_effect=lambda *_, **__: Provider()) as zhipu, \
                    TestClient(app) as client:
                client.post('/api/bridge/session', json={'token': app.state.local_session})
                parent_id, child_id = str(uuid4()), str(uuid4())
                response = client.post('/api/bridge/outputs', json={'request_id': parent_id,
                    'source_id': SOURCE_ID, 'user_request': '说明这几种表示的区别',
                    'output_kind': 'source_answer', 'allow_model_text': True})
                self.assertEqual(response.status_code, 200, response.text)
                app.state.outputs.wait(parent_id, 10)
                response = client.get('/api/bridge/outputs/' + parent_id)
                self.assertEqual(response.status_code, 200, response.text)
                parent = response.json()
                self.assertEqual(len(parent['explanations'][0]['panels']), 2)
                svg = client.get(parent['explanations'][0]['panels'][0]['svg_url'])
                self.assertEqual(svg.status_code, 200)
                self.assertIn('image/svg+xml', svg.headers['content-type'])
                self.assertEqual(svg.headers['x-content-type-options'], 'nosniff')
                response = client.post('/api/bridge/revisions', json={'request_id': child_id,
                    'parent_job_id': parent_id,
                    'expected_parent_fingerprint': parent['revision']['record_fingerprint'],
                    'instruction': '只修改第一幅图的说明，其他原文保留', 'allow_model_text': True})
                self.assertEqual(response.status_code, 200, response.text)
                app.state.outputs.wait(child_id, 10)
                response = client.get('/api/bridge/outputs/' + child_id)
                self.assertEqual(response.status_code, 200, response.text)
                child = response.json()
                self.assertEqual(codex.call_count, 2, 'v16 revisions must keep the frozen writer selection')
                self.assertFalse(zhipu.called, 'Do not silently switch the saved task to a different provider')
                self.assertEqual(child['explanations'][0]['panels'][0]['explanation'], '更新说明，原文仍保留。')
                self.assertIn('更新说明，原文仍保留', child['result']['markdown'])
                self.assertIn('两者来源相同，对象不同。', child['result']['markdown'])
                self.assertNotIn(parent_id, child['result']['markdown'])
                self.assertEqual(client.get('/api/bridge/outputs/' + parent_id).json(), parent)
                self.assertEqual(len(calls), 2)
                exported = client.get('/api/bridge/notes/' + child_id + '/download')
                self.assertEqual(exported.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
                    self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
                    self.assertIn('更新说明，原文仍保留', archive.read('notes.md').decode())
                self.assertFalse(child['quality_approved'])
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
