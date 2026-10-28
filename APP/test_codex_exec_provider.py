"""Key subprocess protocol counterexamples; never run a real model or login."""
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


FIXTURE = r'''
import json, os, pathlib, sys, time
if os.environ['FIXTURE_MODE'] == 'auth':
    sys.stderr.write('Logged in using ChatGPT\n')
    raise SystemExit(0)
data = sys.stdin.buffer.read()
root = pathlib.Path(os.environ['FIXTURE_ROOT'])
(root/'received.bin').write_bytes(data)
(root/'fixture-ready').write_text('ready')
if os.environ['FIXTURE_MODE'] == 'hang':
    time.sleep(30)
else:
    pathlib.Path(os.environ['FIXTURE_RESULT']).write_text('{"answer":"测试正文"}',encoding='utf-8')
    print(json.dumps({'type':'thread.started','thread_id':'fixture-thread'}),flush=True)
    print(json.dumps({'type':'turn.started'}),flush=True)
    if os.environ['FIXTURE_MODE'] == 'failed':
        print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':4}}),flush=True)
        raise SystemExit(17)
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':4,
        'cached_input_tokens':2,'reasoning_output_tokens':1}}),flush=True)
'''


class CodexExecProviderTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('codex_exec_provider'),
                             'controlled local writer provider has not been implemented')
        return importlib.import_module('codex_exec_provider')

    def runtime(self, module, root, mode):
        fixture = root/'fixture.py'
        fixture.write_text(FIXTURE, encoding='utf-8')
        original = subprocess.Popen
        calls = []
        children = []

        def spawn(args, *, cwd, env, **kwargs):
            # Capture the production command/environment, then substitute only
            # the OS boundary with a real, deterministic child process.
            calls.append((list(args), dict(env)))
            child_env = dict(env, FIXTURE_MODE='auth' if 'login' in args else mode,
                             FIXTURE_ROOT=str(root))
            if 'exec' in args:
                child_env['FIXTURE_RESULT'] = args[args.index('--output-last-message') + 1]
            child = original([sys.executable, str(fixture)], cwd=cwd, env=child_env, **kwargs)
            children.append(child)
            return child

        return calls, children, patch.multiple(module, AUTH_HOME=root/'auth-home', RUNS_ROOT=root/'runs'), patch.object(module, '_spawn', side_effect=spawn)

    def test_success_is_scoped_preserves_input_and_reports_only_provider_counters(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls, children, locations, boundary = self.runtime(module, root, 'success')
            with locations, boundary, patch.dict(os.environ, {'DEEPSEEK_API_KEY':'secret-must-not-inherit',
                                                               'CODEX_SQLITE_HOME':'outside-state'}):
                provider = module.CodexExecProvider()
                reply = provider.complete('冻结的系统规格', '来源正文\n本次问题')
            self.assertEqual(json.loads(reply), {'answer':'测试正文'})
            self.assertEqual((root/'received.bin').read_bytes(), '来源正文\n本次问题'.encode())
            writer_calls = [call for call in calls if 'exec' in call[0]]
            self.assertEqual(len(writer_calls), 1)
            args, environment = writer_calls[0]
            self.assertIn('--ignore-user-config', args)
            self.assertIn('--ephemeral', args)
            self.assertIn('model_provider="video_writer_openai_http"', args)
            self.assertIn('model_providers.video_writer_openai_http.supports_websockets=false', args)
            self.assertIn('model_providers.video_writer_openai_http.requires_openai_auth=true', args)
            self.assertFalse(any('model_providers.video_writer_openai_http.base_url=' in arg
                                 for arg in args), 'Production must retain the official auth-resolved endpoint')
            self.assertEqual(environment['CODEX_HOME'], str(root/'auth-home'))
            self.assertNotIn('DEEPSEEK_API_KEY', environment)
            self.assertNotIn('CODEX_SQLITE_HOME', environment)
            self.assertNotIn('secret-must-not-inherit', json.dumps(provider.last_call_metrics))
            self.assertEqual(provider.last_call_metrics['usage']['input_tokens'], 10)
            self.assertEqual(provider.last_call_metrics['usage']['cached_input_tokens'], 2)
            self.assertEqual(provider.last_call_metrics['transport_sessions'], 1)
            self.assertEqual(provider.last_call_metrics['status'], 'succeeded')
            self.assertTrue(all(child.poll() is not None for child in children))

    def test_failed_session_does_not_adopt_existing_final_file_or_replay(self):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls, _, locations, boundary = self.runtime(module, root, 'failed')
            with locations, boundary:
                provider = module.CodexExecProvider()
                with self.assertRaises(module.CodexExecProviderError) as caught:
                    provider.complete('system', 'source')
            self.assertEqual(caught.exception.code, 'writer_failed_or_unknown')
            self.assertNotIn('private diagnostic', str(caught.exception))
            self.assertEqual(sum('exec' in args for args, _ in calls), 1)
            self.assertEqual(provider.last_call_metrics['status'], 'failed')
            self.assertIsNotNone(provider.last_call_metrics['usage'])
            self.assertEqual(provider.last_call_metrics['usage']['input_tokens'],10)
            self.assertEqual(len(list((root/'runs').rglob('final.json'))), 1,
                             'Fixture really wrote a plausible final file before failing')

    def test_cancellation_and_timeout_stop_child_and_never_replay(self):
        module = self.module()
        for cancel in (True, False):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                calls, children, locations, boundary = self.runtime(module, root, 'hang')
                cancelled = lambda: cancel and (root/'fixture-ready').exists()
                with locations, boundary:
                    provider = module.CodexExecProvider(cancelled=cancelled, timeout_seconds=1.0)
                    started = time.monotonic()
                    with self.assertRaises(module.CodexExecProviderError) as caught:
                        provider.complete('system', 'source')
                self.assertEqual(caught.exception.code, 'writer_cancelled' if cancel else 'writer_timeout_unknown')
                self.assertLess(time.monotonic() - started, 8)
                self.assertEqual(sum('exec' in args for args, _ in calls), 1)
                self.assertTrue(all(child.poll() is not None for child in children))

    def test_nonfatal_error_item_keeps_completed_turn_and_redacted_diagnostic(self):
        module = self.module()
        provider = module.CodexExecProvider()
        provider._last_call_metrics = {'usage':None}
        message = 'A recoverable runtime notice with opaque-secret-do-not-log'
        events = [{'type':'item.completed','item':{'type':'error','message':message}},
                  {'type':'turn.completed','usage':{'input_tokens':19,'output_tokens':3}}]
        provider._events(('\n'.join(json.dumps(e) for e in events)+'\n').encode())
        self.assertEqual(provider.last_call_metrics['usage']['input_tokens'], 19)
        self.assertEqual(len(provider.last_call_metrics['event_summary']['notices']), 1)
        self.assertNotIn('opaque-secret-do-not-log', json.dumps(provider.last_call_metrics))

    def test_valid_jsonl_uses_physical_lf_and_keeps_unicode_line_separator_in_text(self):
        module = self.module()
        provider = module.CodexExecProvider()
        provider._last_call_metrics = {'usage':None}
        events = [{'type':'item.completed','item':{'type':'agent_message','text':'第一段\u2028第二段'}},
                  {'type':'turn.completed','usage':{'input_tokens':11,'output_tokens':2}}]
        physical_lines = [json.dumps(e,ensure_ascii=False).encode('utf-8') for e in events]
        self.assertTrue(all(isinstance(json.loads(line),dict) for line in physical_lines))
        provider._events(b'\n'.join(physical_lines)+b'\n')
        self.assertEqual(provider.last_call_metrics['usage']['input_tokens'],11)
        self.assertEqual(provider.last_call_metrics['event_summary']['json_lines'],2)

    def test_top_error_is_diagnostic_when_turn_completed_but_not_success_by_itself(self):
        module = self.module()
        provider = module.CodexExecProvider()
        provider._last_call_metrics = {'usage':None}
        diagnostic = {'type':'error','message':'Stream disconnected; reconnecting. opaque-secret'}
        events = [diagnostic,{'type':'turn.completed','usage':{'input_tokens':13,'output_tokens':2}}]
        provider._events(('\n'.join(json.dumps(e) for e in events)+'\n').encode())
        self.assertEqual(provider.last_call_metrics['usage']['input_tokens'],13)
        self.assertEqual(provider.last_call_metrics['event_summary']['event_types']['error'],1)
        self.assertNotIn('opaque-secret',json.dumps(provider.last_call_metrics))
        provider._last_call_metrics = {'usage':None}
        with self.assertRaises(module.CodexExecProviderError):
            provider._events(json.dumps(diagnostic).encode())

    def test_fatal_or_ignored_configuration_is_rejected_but_reported_usage_retained(self):
        module = self.module()
        for first, code in (({'type':'turn.failed'},'writer_failed_or_unknown'),
                ({'type':'item.completed','item':{'type':'collab_tool_call'}},'writer_failed_or_unknown'),
                ({'type':'item.completed','item':{'type':'error',
                  'message':'Codex is ignoring an unrecognized configuration setting: features.code_mode_host'}},
                 'writer_configuration_rejected')):
            with self.subTest(code=code, event=first['type']):
                provider = module.CodexExecProvider()
                provider._last_call_metrics = {'usage':None}
                events = [first,{'type':'turn.completed','usage':{'input_tokens':7,'output_tokens':2}}]
                with self.assertRaises(module.CodexExecProviderError) as caught:
                    provider._events(('\n'.join(json.dumps(e) for e in events)+'\n').encode())
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(provider.last_call_metrics['usage']['input_tokens'], 7)


if __name__ == '__main__':
    unittest.main()
