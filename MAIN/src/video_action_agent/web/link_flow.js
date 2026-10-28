'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const kinds = ['structured_notes', 'method_list', 'source_answer'];
  const kindNames = {structured_notes: '学习笔记', method_list: '方法清单', source_answer: '针对内容提问'};
  const activeStates = ['queued', 'extracting', 'building'];
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  let flow = null, enabled = false, connected = false, busy = false, polling = false, sequence = 0;
  let pendingId = '', pendingRequest = null, scope = '';
  const active = value => !!value && activeStates.includes(value.state);
  const kindOf = value => kinds.includes(value) ? value : 'structured_notes';
  const element = (tag, text) => { const node = document.createElement(tag); node.textContent = text; return node; };
  const truncate = (text, limit) => text.length > limit ? text.slice(0, limit) + '…' : text;
  const recoveryKey = () => 'vaa-flow-pending-' + scope;
  const scopeReady = () => /^[0-9a-f]{64}$/.test(scope);
  const failureNames = {
    source_unavailable: '来源暂时无法读取', source_mismatch: '来源身份与指定分P不匹配', duration_limit: '音轨时长超出当前处理范围',
    timeout: '读取超时', transcription_timeout: '本机识别超时', decode_failed: '音频解码失败', transcription_failed: '语音识别失败',
    no_speech_recognized: '未识别到语音', empty_audio: '音频没有可识别数据', worker_unavailable: '本机处理进程暂不可用',
    worker_closed: '本机处理进程已关闭', local_runtime_unavailable: '本机识别环境不可用', source_bundle_unavailable: '来源候选未能安全保存',
    invalid_worker_result: '识别结果未通过校验', intake_failed: '来源读取失败', model_text_permission_required: '服务未收到模型使用授权'
  };
  const failureText = code => failureNames[code] || (typeof code === 'string' && code ? '失败类型 ' + code : '原因未说明');
  const where = value => [typeof value.bvid === 'string' && value.bvid, Number.isInteger(value.page) ? 'p=' + value.page : ''].filter(Boolean).join(' / ');
  const openable = value => !!value && value.state === 'available' && value.result_available === true
    && typeof value.notes_job_id === 'string' && uuid.test(value.notes_job_id);
  function statusText(value) {
    const states = {
      queued: '已接收，等待开始提取',
      extracting: '提取中：正在下载指定分P音轨并在本机识别，音频不上传',
      building: '生成中：正在把识别文字发送给已配置模型，生成所选成果候选',
      available: '成果已保存待核对，可打开查看或下载',
      canceled: '已停止后续处理；已发生的音轨读取无法撤销',
      interrupted: '上次运行结果未确认，未自动重试，可查询进度核对'
    };
    if (value.state === 'failed') return '本次处理失败，不会自动重试；已发生的读取无法撤销';
    return states[value.state] || '状态待确认';
  }
  const rowStatus = value => value.state === 'failed'
    ? '本次处理失败（' + failureText(value.error_code) + '），不会自动重试' : statusText(value);
  async function api(path, options = {}) {
    const response = await fetch(path, {...options, headers: {'Content-Type': 'application/json'}, cache: 'no-store'});
    if (!response.ok) {
      const error = new Error(response.status === 401 ? '会话已失效，请重新连接本机助手。' : '操作未成功，请刷新核对当前状态。');
      error.status = response.status;
      const body = await response.json().catch(() => null);
      const code = [body?.error_code, body?.detail?.error_code, typeof body?.detail === 'string' ? body.detail : ''].find(value => typeof value === 'string' && value);
      if (code) error.code = code;
      throw error;
    }
    return response.json();
  }
  function hideRetry() { $('flow-retry').hidden = true; $('flow-retry-pending-text').textContent = ''; }
  function showRetry() {
    $('flow-retry').hidden = false;
    $('flow-retry-pending-text').textContent = pendingRequest
      ? `将按原请求编号重试 · 链接：${pendingRequest.source} · 成果类型：${kindNames[kindOf(pendingRequest.output_kind)]} · 要求或问题：${pendingRequest.user_request}` : '';
  }
  function forgetPending(removeStored) {
    if (removeStored && scopeReady()) { try { sessionStorage.removeItem(recoveryKey()); } catch (_) {} }
    pendingId = ''; pendingRequest = null; hideRetry();
  }
  function restorePending(nextScope) {
    if (nextScope === scope) return;
    scope = typeof nextScope === 'string' ? nextScope : '';
    pendingId = ''; pendingRequest = null; hideRetry();
    $('flow-retry-source-permission').checked = false; $('flow-retry-model-permission').checked = false;
    if (!scopeReady()) return;
    let value = null;
    try { value = JSON.parse(sessionStorage.getItem(recoveryKey()) || 'null'); } catch (_) { return; }
    if (!value) return;
    if (typeof value.request_id !== 'string' || !uuid.test(value.request_id)
        || typeof value.source !== 'string' || !value.source.trim()
        || typeof value.user_request !== 'string' || !value.user_request.trim() || value.user_request.length > 4000
        || !kinds.includes(value.output_kind)
        || value.allow_local_audio_read !== true || value.allow_model_text !== true) {
      try { sessionStorage.removeItem(recoveryKey()); } catch (_) {}
      return;
    }
    pendingRequest = value; pendingId = value.request_id;
  }
  function controls() {
    const locked = busy || !!pendingId || active(flow);
    $('flow-start').disabled = !connected || !enabled || locked;
    $('flow-refresh').disabled = !connected || busy;
    $('flow-cancel').disabled = !connected || busy || !active(flow) || typeof flow?.flow_id !== 'string' || !flow.flow_id;
    $('flow-open').disabled = !connected || !openable(flow);
    $('flow-source').disabled = locked; $('flow-output-kind').disabled = locked;
    $('flow-retry-submit').disabled = !connected || !enabled || busy || !pendingRequest
      || !$('flow-retry-source-permission').checked || !$('flow-retry-model-permission').checked;
  }
  function display() {
    if (!connected) { hideRetry(); $('flow-history').replaceChildren(); $('flow-state').textContent = '尚未连接本机助手。'; $('flow-error').textContent = ''; }
    else if (!flow) {
      $('flow-state').textContent = !enabled ? '链接直接生成成果未开启，可使用下方分步流程。'
        : pendingId ? '本次提交结果尚未确认；查询进度会按原编号核对，不会自动重复提交。'
        : '填写B站链接与要求，分别勾选两项许可后提交；本机先提取音轨并识别，再生成所选成果候选。';
      $('flow-error').textContent = '';
    } else {
      $('flow-state').textContent = [where(flow), kindNames[kindOf(flow.output_kind)]].filter(Boolean).join(' · ') + '：' + statusText(flow);
      $('flow-error').textContent = flow.state === 'failed' ? '失败原因：' + failureText(flow.error_code) + '。已保存的材料与成果保留，不会自动重试。' : '';
    }
    controls();
  }
  function openResult(id) {
    if (!connected || typeof id !== 'string' || !uuid.test(id)) return;
    window.dispatchEvent(new CustomEvent('vaa-open-candidate', {detail: id}));
  }
  function historyRows(value) {
    const list = $('flow-history'); list.replaceChildren();
    const rows = Array.isArray(value?.flows) ? value.flows : [];
    for (const row of rows) {
      const item = element('li', '');
      const request = typeof row.user_request === 'string' && row.user_request ? ' · ' + truncate(row.user_request, 80) : '';
      item.append(element('p', [where(row), kindNames[kindOf(row.output_kind)]].filter(Boolean).join(' · ') + request),
        element('p', rowStatus(row)));
      if (openable(row)) {
        const button = element('button', '打开成果'); button.type = 'button';
        button.addEventListener('click', () => openResult(row.notes_job_id));
        item.append(button);
      }
      list.append(item);
    }
    if (!rows.length) list.append(element('li', '暂无已记录的链接直达生成。'));
  }
  async function refresh() {
    if (polling || busy) return;
    polling = true; const ticket = sequence;
    try {
      const session = await api('/app/session');
      if (ticket !== sequence) return;
      if (!session.connected) {
        if (connected) { sequence++; flow = null; }
        connected = false; $('flow-history').replaceChildren(); display(); return;
      }
      connected = true;
      let state = null;
      try { state = await api('/app/flows'); }
      catch (error) {
        if (error.status !== 404) throw error;
        enabled = false; flow = null; hideRetry(); $('flow-history').replaceChildren(); display(); return;
      }
      if (ticket !== sequence) return;
      enabled = state.enabled === true;
      restorePending(state.scope_id);
      if (!enabled) { flow = null; hideRetry(); $('flow-history').replaceChildren(); display(); return; }
      if (pendingId) {
        let found = null, missing = false;
        try { found = await api('/app/flows/' + encodeURIComponent(pendingId)); }
        catch (error) {
          if (error.status !== 404) throw error;
          missing = true;
        }
        if (ticket !== sequence) return;
        if (missing) { flow = null; showRetry(); }
        else { flow = found; forgetPending(true); }
      } else { flow = state.flow ?? null; hideRetry(); }
      display();
      const history = await api('/app/flows/history');
      if (ticket !== sequence) return;
      historyRows(history);
    } catch (error) {
      if (ticket !== sequence) return;
      if (error.status === 401) { connected = false; sequence++; flow = null; display(); return; }
      $('flow-error').textContent = '链接直达生成的状态暂时无法确认，请稍后点击“查询进度”；不会自动重试提交，也不会把未知当作失败。';
    } finally { polling = false; controls(); }
  }
  async function sendFlow(request, ticket) {
    busy = true; flow = null; controls();
    let failureMessage = '';
    $('flow-state').textContent = '正在提交链接直达生成…';
    try {
      const value = await api('/app/flows', {method: 'POST', body: JSON.stringify(request)});
      if (ticket !== sequence || !connected) return;
      forgetPending(true);
      $('flow-source-permission').checked = false; $('flow-model-permission').checked = false;
      $('flow-retry-source-permission').checked = false; $('flow-retry-model-permission').checked = false;
      flow = value ?? null; $('flow-error').textContent = '';
    } catch (error) {
      if (ticket !== sequence) return;
      const settled = !!error.status && error.status < 500;
      if (settled) forgetPending(true);
      failureMessage = settled
        ? (error.status === 401 ? '连接已失效，请重新连接本机助手；本次提交未生效。' : '提交被拒绝：' + (error.code || error.message) + '。可核对后重新提交。')
        : '提交结果待确认：已保留原请求编号，点击“查询进度”核对；不会自动重复提交。';
      if (error.status === 401) { connected = false; sequence++; flow = null; }
    } finally { busy = false; display(); if (failureMessage && connected) $('flow-error').textContent = failureMessage; controls(); }
  }
  $('flow-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!connected || !enabled || busy || pendingId || active(flow) || !scopeReady() || !$('flow-form').reportValidity()) return;
    if (!$('flow-source-permission').checked || !$('flow-model-permission').checked) {
      $('flow-error').textContent = '请先分别勾选两项许可：允许本机提取音轨并识别，以及允许把识别文字交给模型生成。'; return;
    }
    const request = {request_id: crypto.randomUUID(), source: $('flow-source').value.trim(),
      user_request: $('flow-request').value.trim(), output_kind: kindOf($('flow-output-kind').value),
      allow_local_audio_read: true, allow_model_text: true};
    try { sessionStorage.setItem(recoveryKey(), JSON.stringify(request)); }
    catch (_) { $('flow-error').textContent = '浏览器无法暂存这次请求，本次未提交，没有发出任何处理请求。请允许此页面使用本地会话存储后再试。'; return; }
    sequence++; const ticket = sequence;
    pendingId = request.request_id; pendingRequest = request;
    $('flow-error').textContent = '';
    await sendFlow(request, ticket);
  });
  $('flow-retry-submit').addEventListener('click', async () => {
    if (!connected || !enabled || busy || !pendingId || !pendingRequest
        || !$('flow-retry-source-permission').checked || !$('flow-retry-model-permission').checked) return;
    sequence++; const ticket = sequence;
    $('flow-error').textContent = '';
    await sendFlow(pendingRequest, ticket);
  });
  $('flow-cancel').addEventListener('click', async () => {
    if (!connected || busy || !active(flow) || typeof flow.flow_id !== 'string' || !flow.flow_id) return;
    const ticket = ++sequence, identity = flow.flow_id; busy = true; controls();
    let failureMessage = '';
    try {
      const value = await api('/app/flows/' + encodeURIComponent(identity) + '/cancel', {method: 'POST'});
      if (ticket !== sequence || !connected) return;
      flow = value ?? flow;
    } catch (error) {
      if (ticket !== sequence) return;
      failureMessage = error.status === 401 ? '连接已失效，请重新连接本机助手。' : '取消结果待确认，请点击“查询进度”核对；已发生的音轨读取无法撤销。';
      if (error.status === 401) { connected = false; sequence++; flow = null; }
    } finally { busy = false; display(); if (failureMessage && connected) $('flow-error').textContent = failureMessage; controls(); }
  });
  $('flow-refresh').addEventListener('click', refresh);
  $('flow-open').addEventListener('click', () => { if (openable(flow)) openResult(flow.notes_job_id); });
  const revoke = () => { $('flow-source-permission').checked = false; $('flow-model-permission').checked = false; };
  $('flow-source').addEventListener('input', revoke);
  $('flow-request').addEventListener('input', revoke);
  const kindHints = {source_answer: '将依据本次提取识别的材料，回答你在下方明确写下的问题；问题必填，不会自动替你拟定问题，也不读取这个链接之外的内容。'};
  function syncKindHint() {
    const kind = kindOf($('flow-output-kind').value);
    $('flow-kind-hint').textContent = kindHints[kind] || '';
    $('flow-kind-hint').hidden = !kindHints[kind];
    $('flow-request').placeholder = kind === 'source_answer' ? '例如：视频里提到的某个操作具体怎么做？请只依据材料回答' : '提取课程的概念、步骤、例子和注意事项，保留原文依据';
  }
  $('flow-output-kind').addEventListener('change', () => { syncKindHint(); revoke(); });
  $('flow-retry-source-permission').addEventListener('change', controls);
  $('flow-retry-model-permission').addEventListener('change', controls);
  syncKindHint();
  display(); refresh(); setInterval(refresh, 3000);
})();
