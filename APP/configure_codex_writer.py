"""Official sign-in for the isolated optional writer; no model or source call."""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from codex_exec_provider import AUTH_HOME, BINARY, CodexExecProvider, CodexExecProviderError

provider = CodexExecProvider()
provider._runtime()
environment = provider._environment()
status = AUTH_HOME/'login-attempt.json'
if status.exists():
    raise SystemExit('Existing login attempt retained; inspect its real process before another sign-in.')
record = {'started_at':datetime.now(timezone.utc).isoformat(), 'helper_pid':os.getpid(),
          'scope':'official_login_in_separate_home', 'model_dispatched':False,
          'global_auth_copied_or_linked':False, 'state':'starting'}
with status.open('x',encoding='utf-8') as target:
    json.dump(record,target,ensure_ascii=False,indent=2)


def write():
    temporary = status.with_suffix('.tmp')
    temporary.write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(temporary,status)


try:
    # The official client opens its own normal browser flow. Raw short-lived login
    # URLs remain in this private runtime log, never in product/API diagnostics.
    with (AUTH_HOME/'official-login-private.log').open('wb') as log:
        child = subprocess.Popen([str(BINARY),'-c','analytics.enabled=false','-c',
            'cli_auth_credentials_store="file"','login'],cwd=AUTH_HOME,env=environment,
            stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        record.update(state='official_login_running',process_id=child.pid)
        write()
        code = child.wait()
    record.update(exit_code=code,state='login_exited')
    if code == 0:
        try:
            provider._authenticated(environment)
        except CodexExecProviderError as exc:
            record.update(state='authentication_not_verified',error_code=exc.code)
        else:
            record['state']='official_authentication_verified'
except Exception as exc:
    record.update(state='login_failed',error_type=type(exc).__name__)
finally:
    record['updated_at']=datetime.now(timezone.utc).isoformat()
    write()
print(json.dumps({key:value for key,value in record.items() if 'url' not in key},ensure_ascii=False))
