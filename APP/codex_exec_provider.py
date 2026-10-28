"""A pinned, independently authenticated local Codex writing component.

The wrapper never retries or imports other service keys. Upstream transport
retries are not claimed to be measured or zero. Native publication fences remain
the authority over cancellation, source changes, and late results.
"""
import copy
import hashlib
import json
import math
import os
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

MODEL = 'gpt-6.1-sol'
VERSION = '0.163.0'
TRANSPORT_VERSION = 1
ROOT = Path(__file__).resolve().parent
BINARY = Path('D:/CodexWork/VideoActionAgent-value-completion-20261024/codex-writer-runtime/Lib/site-packages/codex_cli_bin/bin/codex.exe')
BINARY_SHA256 = '2692c8c7bb2cf8b0aefe638741fc90236f4b824c59578ce84994f9de58200993'
AUTH_HOME = ROOT/'runtime/codex-writer-home'
RUNS_ROOT = ROOT/'runtime/codex-writer-runs'
INSTRUCTIONS = ROOT/'codex_writer_instructions.txt'
_LIMIT = 2_000_000
_ENVIRONMENT_KEYS = frozenset({'SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATH',
    'PATHEXT', 'TEMP', 'TMP', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'PROGRAMDATA',
    'NUMBER_OF_PROCESSORS'})
_DISABLED = ('shell_tool', 'shell_snapshot', 'apps', 'plugins', 'remote_plugin', 'memories',
    'multi_agent', 'multi_agent_v2', 'goals', 'hooks', 'browser_use', 'browser_use_external',
    'computer_use', 'in_app_browser', 'image_generation', 'view_image', 'request_permissions_tool',
    'code_mode', 'code_mode_only', 'code_mode_host', 'skill_mcp_dependency_install',
    'skill_search', 'sleep_tool', 'tool_suggest')


class CodexExecProviderError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _spawn(args, **kwargs):
    return subprocess.Popen(args, **kwargs)


def _directory(path):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
        raise CodexExecProviderError('writer_runtime_unavailable')
    if any(part.is_symlink() or part.is_junction() for part in (path, *path.parents)):
        raise CodexExecProviderError('writer_runtime_unavailable')
    path.mkdir(parents=True, exist_ok=True)
    return path


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _loads(text):
    return json.loads(text, object_pairs_hook=_unique,
                      parse_constant=lambda _value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


class CodexExecProvider:
    model_id = MODEL

    def __init__(self, *, cancelled=None, timeout_seconds=600):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= 600):
            raise CodexExecProviderError('invalid_timeout')
        self.cancelled = cancelled or (lambda: False)
        self.timeout_seconds = timeout_seconds
        self._last_call_metrics = None

    @property
    def last_call_metrics(self):
        return copy.deepcopy(self._last_call_metrics)

    def _environment(self):
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in _ENVIRONMENT_KEYS}
        environment['CODEX_HOME'] = str(AUTH_HOME)
        return environment

    def _runtime(self):
        _directory(AUTH_HOME)
        _directory(RUNS_ROOT)
        if (BINARY.is_symlink() or BINARY.is_junction() or not BINARY.is_file()
                or not INSTRUCTIONS.is_file() or INSTRUCTIONS.is_symlink()):
            raise CodexExecProviderError('writer_runtime_unavailable')
        with BINARY.open('rb') as binary:
            if hashlib.file_digest(binary, 'sha256').hexdigest() != BINARY_SHA256:
                raise CodexExecProviderError('writer_runtime_unavailable')
        # Authentication is managed only by the official client in this home.
        # A config file is unnecessary: all execution settings are per-process.
        if (AUTH_HOME/'config.toml').exists():
            raise CodexExecProviderError('writer_home_configuration_unexpected')

    def _authenticated(self, environment):
        process = _spawn([str(BINARY), '-c', 'analytics.enabled=false', '-c',
                          'cli_auth_credentials_store="file"', 'login', 'status'],
                         cwd=AUTH_HOME, env=environment, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise CodexExecProviderError('writer_authentication_unconfirmed') from None
        if process.returncode != 0:
            raise CodexExecProviderError('writer_authentication_required')

    def command(self, system, workspace, result):
        """Fixed execution policy; HTTP callers cannot choose a command or model."""
        settings = [
            'model=' + json.dumps(MODEL), 'model_reasoning_effort="high"',
            # Keep official client authentication and its default destination;
            # choose HTTP directly instead of a fresh WebSocket fallback per run.
            'model_provider="video_writer_openai_http"',
            'model_providers.video_writer_openai_http.name="OpenAI"',
            'model_providers.video_writer_openai_http.requires_openai_auth=true',
            'model_providers.video_writer_openai_http.supports_websockets=false',
            'approval_policy="never"', 'agents.enabled=false',
            'agents.max_concurrent_threads_per_session=1', 'analytics.enabled=false',
            'cli_auth_credentials_store="file"',
            'web_search="disabled"', 'mcp_servers={}', 'apps._default.enabled=false',
            'project_doc_max_bytes=0', 'skills.include_instructions=false',
            'skills.bundled.enabled=false', 'features.skip_host_skill_discovery=true',
            'suppress_unstable_features_warning=true',
            'history.persistence="none"',
            'sqlite_home=' + json.dumps((AUTH_HOME/'state').as_posix()),
            'log_dir=' + json.dumps((AUTH_HOME/'logs').as_posix()),
            'default_permissions="video_writer"',
            'permissions.video_writer.filesystem={":root"="deny",'
            + json.dumps(workspace.as_posix()) + '="read"}',
            'permissions.video_writer.network.enabled=false',
            'model_instructions_file=' + json.dumps(INSTRUCTIONS.as_posix()),
            'developer_instructions=' + json.dumps(system, ensure_ascii=False),
            *['features.' + feature + '=false' for feature in _DISABLED],
        ]
        args = [str(BINARY)]
        for setting in settings:
            args.extend(['--config', setting])
        args.extend(['exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
                     '--skip-git-repo-check', '--json', '--color', 'never', '--cd', str(workspace),
                     '--output-last-message', str(result), '-'])
        if len(subprocess.list2cmdline(args).encode('utf-16-le')) // 2 >= 32767:
            raise CodexExecProviderError('writer_command_over_limit')
        return args

    def _run(self, args, environment, workspace, user):
        process = _spawn(args, cwd=workspace, env=environment, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        self._last_call_metrics['transport_sessions'] = 1
        buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        faults = []

        def receive(name, pipe):
            try:
                while chunk := pipe.read(65536):
                    buffers[name].extend(chunk)
                    if len(buffers[name]) > _LIMIT:
                        faults.append('writer_output_over_limit')
                        break
            except (OSError, ValueError):
                faults.append('writer_io_unknown')
            finally:
                pipe.close()

        def send():
            try:
                process.stdin.write(user.encode('utf-8'))
            except (OSError, ValueError):
                faults.append('writer_io_unknown')
            finally:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass

        threads = [threading.Thread(target=receive, args=(name, getattr(process, name)), daemon=True)
                   for name in buffers]
        threads.append(threading.Thread(target=send, daemon=True))
        for thread in threads:
            thread.start()
        started = time.monotonic()
        try:
            while True:
                if self.cancelled():
                    raise CodexExecProviderError('writer_cancelled')
                if faults:
                    raise CodexExecProviderError(faults[0])
                if time.monotonic() - started >= self.timeout_seconds:
                    raise CodexExecProviderError('writer_timeout_unknown')
                if process.poll() is not None:
                    break
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=2)
            self._last_call_metrics.update(exit_code=process.returncode,
                stdout_bytes=len(buffers['stdout']), stderr_bytes=len(buffers['stderr']))
        if faults:
            raise CodexExecProviderError(faults[0])
        return bytes(buffers['stdout'])

    def _events(self, raw):
        # JSONL is framed by physical LF, not every Unicode line separator.
        # In this pinned CLI, both ErrorItem and retrying StreamError can be
        # diagnostics. The JSONL exporter loses will_retry on top-level error;
        # turn.completed/failed supplies the terminal status. Configuration is
        # a separate permission gate, not a redefinition of a runtime notice.
        completed = []
        summary = {'json_lines':0,'event_types':{},'notices':[],'notice_count':0,
                   'malformed_lines':0}
        self._last_call_metrics['event_summary'] = summary
        fatal = configuration_rejected = tool_activity = False
        known_types = {'thread.started','turn.started','turn.completed','turn.failed',
                       'item.started','item.updated','item.completed','error'}
        def notice(message, origin):
            nonlocal configuration_rejected
            if not isinstance(message,str):
                raise ValueError('invalid notice')
            lower = message.lower()
            ignored = ('unrecognized configuration' in lower or 'unknown feature' in lower
                or ('configuration' in lower and 'ignoring' in lower))
            configuration_rejected = configuration_rejected or ignored
            category = ('configuration_not_applied' if ignored else
                'code_host_disabled' if message.startswith('Code Mode is unavailable because code-mode host is disabled.') else
                'mentions_websocket' if 'websocket' in lower else
                'mentions_reconnecting' if 'reconnect' in lower else
                'mentions_stream_interruption' if 'stream' in lower and ('disconnect' in lower or 'interrupt' in lower) else
                'mentions_usage_limit' if 'rate limit' in lower or 'quota' in lower else
                'top_error_unclassified' if origin == 'error' else 'nonfatal_item_error')
            summary['notice_count'] += 1
            if len(summary['notices']) < 8:
                summary['notices'].append({'category':category,'origin':origin,
                    'sha256':hashlib.sha256(message.encode('utf-8')).hexdigest()})

        for line in raw.split(b'\n'):
            if not line.strip():
                continue
            summary['json_lines'] += 1
            try:
                event = _loads(line.decode('utf-8'))
                if not isinstance(event,dict):
                    raise ValueError('invalid event')
                kind = event.get('type')
                category = kind if isinstance(kind,str) and kind in known_types else 'other'
                summary['event_types'][category] = summary['event_types'].get(category,0)+1
                fatal = fatal or kind == 'turn.failed'
                if kind == 'error':
                    notice(event.get('message',''),'error')
                if kind in {'item.started','item.updated','item.completed'}:
                    item = event.get('item', {})
                    if not isinstance(item,dict):
                        raise ValueError('invalid item')
                    if item.get('type') == 'error' and kind == 'item.completed':
                        notice(item.get('message',''),'item.error')
                    tool_activity = tool_activity or item.get('type') in {
                        'command_execution','file_change','mcp_tool_call','web_search','collab_tool_call','collaboration_tool_call'}
                if kind == 'turn.completed':
                    completed.append(event)
            except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
                summary['malformed_lines'] += 1
        summary['completion_count'] = len(completed)
        usage = completed[0].get('usage') if len(completed) == 1 else None
        if isinstance(usage,dict):
            self._last_call_metrics['usage'] = {key: value for key, value in usage.items()
                if key in {'input_tokens', 'output_tokens', 'cached_input_tokens',
                           'cache_write_input_tokens', 'reasoning_output_tokens'}
                and type(value) is int and value >= 0} or None
        if configuration_rejected:
            summary['rejection_reason'] = 'configuration_not_applied'
            raise CodexExecProviderError('writer_configuration_rejected')
        reason = ('fatal_event' if fatal else 'unallowed_tool_event' if tool_activity else
            'invalid_event_stream' if summary['malformed_lines'] else
            'completion_count_mismatch' if len(completed) != 1 else None)
        if reason:
            summary['rejection_reason'] = reason
            raise CodexExecProviderError('writer_failed_or_unknown')

    def complete(self, system, user):
        started = time.monotonic()
        self._last_call_metrics = {'provider': 'codex_local', 'model': MODEL, 'reasoning_effort': 'high',
            'cli_version': VERSION, 'transport_version': TRANSPORT_VERSION, 'status': 'running',
            'responses_transport': 'http',
            'transport_sessions': 0, 'wrapper_retry': False, 'upstream_request_count': None,
            'elapsed_seconds': None, 'usage': None, 'error_code': None,'phase':'validate'}
        try:
            if (not isinstance(system, str) or not system.strip() or len(system) > 12000
                    or not isinstance(user, str) or not user.strip() or len(user.encode('utf-8')) > _LIMIT):
                raise CodexExecProviderError('invalid_input')
            if self.cancelled():
                raise CodexExecProviderError('writer_cancelled')
            self._runtime()
            environment = self._environment()
            self._last_call_metrics['phase'] = 'authentication'
            self._authenticated(environment)
            if self.cancelled():
                raise CodexExecProviderError('writer_cancelled')
            directory = _directory(RUNS_ROOT / str(uuid4()))
            self._last_call_metrics['run_id'] = directory.name
            workspace = _directory(directory/'workspace')
            result = directory/'final.json'
            args = self.command(system, workspace, result)
            self._last_call_metrics['execution_policy_sha256'] = hashlib.sha256(
                json.dumps({'args': args[1:-1], 'base_instructions_sha256': hashlib.sha256(
                    INSTRUCTIONS.read_bytes()).hexdigest(), 'binary_sha256': BINARY_SHA256},
                    ensure_ascii=False).encode()).hexdigest()
            self._last_call_metrics['phase'] = 'writer_process'
            events = self._run(args,environment,workspace,user)
            self._last_call_metrics['phase'] = 'event_validation'
            self._events(events)
            if self._last_call_metrics.get('exit_code') != 0:
                raise CodexExecProviderError('writer_failed_or_unknown')
            if self.cancelled():
                raise CodexExecProviderError('writer_cancelled')
            self._last_call_metrics['phase'] = 'final_validation'
            if result.is_symlink() or not result.is_file() or result.stat().st_size > _LIMIT:
                raise CodexExecProviderError('writer_final_invalid')
            reply = result.read_text(encoding='utf-8')
            if not isinstance(_loads(reply), dict):
                raise CodexExecProviderError('writer_final_invalid')
            self._last_call_metrics['status'] = 'succeeded'
            self._last_call_metrics['phase'] = 'completed'
            return reply
        except CodexExecProviderError as exc:
            self._last_call_metrics.update(status='failed', error_code=exc.code)
            raise
        except Exception:
            self._last_call_metrics.update(status='failed', error_code='writer_failed_or_unknown')
            raise CodexExecProviderError('writer_failed_or_unknown') from None
        finally:
            self._last_call_metrics['elapsed_seconds'] = time.monotonic() - started
