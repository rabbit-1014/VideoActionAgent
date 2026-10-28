"""Bounded writer selection and one fake-provider API round trip, never live inference."""
import copy
import inspect
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
from bridge_contract import digest
from goal_native import policy, validate_policy
from seed_sources import SOURCE_ID


DEEPSEEK = {'id': 'deepseek_official', 'model': 'deepseek-v4-pro', 'reasoning_effort': 'high'}
CODEX = {'id': 'codex_local', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'transport_version': 1}


class WriterPolicyTests(unittest.TestCase):
    def test_default_deepseek_policy_is_unchanged_and_codex_is_explicit(self):
        default = policy()
        self.assertEqual(default['version'], 14)
        self.assertEqual(default['generation_provider'], DEEPSEEK)
        validate_policy(default)
        self.assertIn('writer', inspect.signature(policy).parameters,
                      'The optional writer selector has not been implemented')
        self.assertEqual(policy(writer='deepseek'), default)
        selected = policy(writer='codex')
        self.assertEqual(selected['version'], 16)
        self.assertEqual(selected['generation_provider'], CODEX)
        self.assertEqual(selected['skill'], default['skill'])
        self.assertEqual(selected['tools']['render_diagram'], default['tools']['render_diagram'])
        self.assertIn('render_explanation', selected['tools'])
        validate_policy(selected)
        self.assertEqual(policy(), default, 'Selecting Codex must not change the default')
        validate_policy(default)

    def test_rehashed_provider_or_model_forgery_and_cross_policy_selection_are_rejected(self):
        self.assertIn('writer', inspect.signature(policy).parameters,
                      'The optional writer selector has not been implemented')
        selected = policy(writer='codex')
        validate_policy(selected)
        for field, value in (('id', 'some_other_provider'), ('model', 'some_unconfigured_model')):
            with self.subTest(field=field):
                forged = copy.deepcopy(selected)
                forged['generation_provider'][field] = value
                forged['sha256'] = digest({key: item for key, item in forged.items() if key != 'sha256'})
                with self.assertRaisesRegex(ValueError, 'invalid_goal_policy'):
                    validate_policy(forged)
        for version, selection in ((14, CODEX), (15, DEEPSEEK)):
            with self.subTest(version=version):
                forged = copy.deepcopy(selected)
                forged.update(version=version, generation_provider=copy.deepcopy(selection))
                forged['sha256'] = digest({key: item for key, item in forged.items() if key != 'sha256'})
                with self.assertRaisesRegex(ValueError, 'invalid_goal_policy'):
                    validate_policy(forged)


class CodexWriterApiTests(unittest.TestCase):
    """One test/app/runtime for the upstream engine's process-global configuration."""

    def test_optional_codex_generation_is_saved_readable_and_frozen_without_replay(self):
        self.assertIn('generation_provider', inspect.signature(build_app).parameters,
                      'The optional application writer selector has not been implemented')
        previous = Path.cwd()
        calls = []

        class Provider:
            def complete(self, system, prompt):
                calls.append({'system': system, 'prompt': json.loads(prompt)})
                return json.dumps({'goal': '说明课程主题', 'success_criteria': ['简短回答当前问题'],
                    'proposed_deliverable': '一句话', 'status': 'ready', 'gaps': [],
                    'markdown': '本课说明如何从文字构建并关联知识图谱。', 'tool_calls': []}, ensure_ascii=False)

        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        runtime = Path(temporary.name)
        app = None
        try:
            app = build_app(runtime, provider_factory=Provider, generation_provider='codex')
            with TestClient(app) as client:
                session = client.post('/api/bridge/session', json={'token': app.state.local_session})
                self.assertEqual(session.status_code, 200, session.text)
                identity = str(uuid4())
                body = {'request_id': identity, 'source_id': SOURCE_ID,
                        'user_request': '一句话说明本课主题，不加图。',
                        'output_kind': 'source_answer', 'allow_model_text': True}
                admitted = client.post('/api/bridge/outputs', json=body)
                self.assertEqual(admitted.status_code, 200, admitted.text)
                app.state.outputs.wait(identity, 10)
                read = client.get('/api/bridge/outputs/' + identity)
                self.assertEqual(read.status_code, 200, read.text)
                candidate = read.json()
                self.assertEqual(candidate['state'], 'available')
                self.assertEqual(candidate['job_id'], identity)
                self.assertEqual(candidate['source_id'], SOURCE_ID)
                self.assertEqual(candidate['user_request'], body['user_request'])
                self.assertEqual(candidate['generation_model'], 'gpt-6.1-sol')
                self.assertIn('从文字构建并关联知识图谱', candidate['result']['markdown'])
                self.assertFalse(candidate['completed'])
                self.assertFalse(candidate['quality_approved'])
                self.assertEqual(len(calls), 1, 'A fake generation must not cause retries or extra review calls')
                self.assertEqual(calls[0]['prompt']['user_request'], body['user_request'])
                self.assertEqual(len(calls[0]['prompt']['source_material_untrusted']['transcription']['segments']), 111)
                binding = app.state.outputs.binding(identity)
                self.assertEqual(binding['goal_policy']['version'], 16)
                self.assertEqual(binding['goal_policy']['generation_provider'], CODEX)
                validate_policy(binding['goal_policy'])
                binding_path = app.state.outputs.bindings / (identity + '.json')
                original_binding = binding_path.read_bytes()
                native_request = runtime / 'native-engine' / 'notes' / 'native' / (identity + '.model-request.json')
                configured = json.loads(native_request.read_bytes())
                self.assertEqual(configured['model'], 'gpt-6.1-sol')
                self.assertEqual(configured['reasoning_effort'], 'high')
                self.assertFalse(configured['stream'])
                replay = client.post('/api/bridge/outputs', json=body)
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(client.get('/api/bridge/outputs/' + identity).json(), candidate)
                self.assertEqual(binding_path.read_bytes(), original_binding)
                self.assertEqual(len(calls), 1, 'An already admitted identity must reuse its frozen result')
        finally:
            if app is not None:
                app.state.outputs.close()
            from sqlalchemy.orm import close_all_sessions
            close_all_sessions()
            engine_module = sys.modules.get('app.db.engine')
            if engine_module is not None:
                engine_module.engine.dispose()
            logging.shutdown()
            os.chdir(previous)
            temporary.cleanup()


if __name__ == '__main__':
    unittest.main()
