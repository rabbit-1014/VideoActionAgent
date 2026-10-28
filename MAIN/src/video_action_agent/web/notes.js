'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const active = value => value && ['queued', 'running', 'cancel_requested'].includes(value.state);
  let job = null, result = null, enabled = false, connected = false, busy = false, sequence = 0;
  let pendingId = '', revisionPendingId = '', revisionJobId = '', polling = false, suppressCurrent = false;
  let revisionRequest = null, revisionScope = '';
  const recoveryKey = () => 'vaa-revision-pending-' + revisionScope;
  function forgetRevision() {
    try { sessionStorage.removeItem(recoveryKey()); } catch (_) {}
    revisionPendingId = ''; revisionRequest = null;
    $('notes-revision-retry-permission').checked = false;
  }
  function restoreRevision(scope) {
    if (scope === revisionScope) return;
    revisionScope = scope || ''; revisionPendingId = ''; revisionRequest = null;
    if (!/^[0-9a-f]{64}$/.test(revisionScope)) return;
    try {
      const value = JSON.parse(sessionStorage.getItem(recoveryKey()) || 'null');
      if (!value) return;
      if (typeof value.parent_id !== 'string' || typeof value.request_id !== 'string'
          || !/^[0-9a-f-]{36}$/.test(value.parent_id) || !/^[0-9a-f-]{36}$/.test(value.request_id)
          || typeof value.expected_parent_fingerprint !== 'string' || !/^[0-9a-f]{64}$/.test(value.expected_parent_fingerprint)
          || typeof value.instruction !== 'string' || !value.instruction.trim() || value.instruction.length > 4000) {
        sessionStorage.removeItem(recoveryKey()); return;
      }
      revisionRequest = value; revisionPendingId = value.request_id;
      $('notes-revision-retry-permission').checked = false;
    } catch (_) { revisionRequest = null; revisionPendingId = ''; }
  }
  const kindOf = value => value === 'method_list' || value === 'source_answer' ? value : 'structured_notes';
  const kindNames = {structured_notes: '学习笔记', method_list: '方法清单', source_answer: '母版问答'};
  function statusText(state, kind) {
    const generic = {queued: '已接收，等待生成', cancel_requested: '已请求取消，等待当前调用返回', canceled: '已取消', failed: '本次生成失败，没有发布成果', interrupted: '上次运行结果未确认，未自动重试'};
    if (state === 'running') return kind === 'source_answer' ? '正在依据母版材料回答问题' : `正在生成${kindNames[kind]}`;
    if (state === 'available') return `成果候选已保存（${kindNames[kind]}），可查看和下载`;
    return generic[state] || '状态待确认';
  }
  const element = (tag, text) => { const node = document.createElement(tag); node.textContent = text; return node; };
  const paragraph = text => { const node = element('p', text); node.style.whiteSpace = 'pre-line'; return node; };
  const skillOf = value => value?.skill?.id ? value.skill : null;
  const revisionRejections = {
    revision_parent_changed: '服务核对发现原稿身份已变化，本次未保存修改；请从成果历史重新打开后再试。',
    revision_parent_superseded: '所选版本已有更新的版本，本次未保存修改；请从下方成果历史打开较新版本查看。',
    revision_history_incomplete: '服务暂时无法核对这份成果的版本历史，本次未保存修改；可稍后从成果历史重新打开再试。',
    notes_request_conflict: '修改请求与当前状态冲突，请刷新核对后再试。',
    model_text_permission_required: '服务未收到本次修改的模型使用授权，请勾选许可后重试。'
  };
  function revisionOpen() {
    const revision = result?.revision;
    return !!(connected && enabled && /^[0-9a-f]{64}$/.test(revisionScope) && revision && Number.isInteger(revision.version) && revision.version >= 1
      && typeof revision.record_fingerprint === 'string' && revision.record_fingerprint && job?.job_id);
  }
  function clear() {
    window.dispatchEvent(new CustomEvent('vaa-candidate-cleared'));
    result = null; $('notes-result').hidden = true; $('notes-content').replaceChildren();
    $('notes-toc-list').replaceChildren(); $('notes-toc').hidden = true;
    $('notes-download-md').disabled = true; $('notes-download-json').disabled = true;
  }
  function controls() {
    $('notes-start').disabled = !connected || !enabled || busy || !!pendingId || !!revisionPendingId || !!active(job) || !$('notes-source').value;
    $('notes-cancel').disabled = !connected || busy || !job || !['queued', 'running'].includes(job.state);
    $('notes-refresh').disabled = busy || !connected;
    $('notes-output-kind').disabled = busy || !!pendingId || !!revisionPendingId || !!active(job);
    $('notes-source').disabled = busy || !!pendingId || !!revisionPendingId || !!active(job);
    $('notes-revision-submit').disabled = !connected || !enabled || busy || !!pendingId || !!revisionPendingId || !!active(job) || !revisionOpen();
    $('notes-revision-recovery').hidden = !connected || !enabled || !revisionRequest || !revisionPendingId;
    $('notes-revision-pending-text').textContent = connected && enabled && revisionRequest ? '原修改要求：' + revisionRequest.instruction : '';
    $('notes-revision-retry').disabled = !connected || !enabled || busy || !!pendingId || !!active(job)
      || !revisionRequest || !$('notes-revision-retry-permission').checked;
    $('notes-availability').textContent = enabled ? '使用已保存母版，不重新下载音轨或识别。' : '本机资料整理未开启。';
  }
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
  function display(value) {
    job = value;
    $('notes-state').textContent = value ? statusText(value.state, kindOf(value.output_kind)) + `；来源 ${value.bvid} / p=${value.page}` : '选择母版并填写需要整理的内容或问题。';
    $('notes-error').textContent = value?.state === 'failed' ? '失败类型：' + value.error_code + '。原母版仍保留，不会自动重试生成。' : '';
    if (value) { try { localStorage.setItem('vaa-notes-job', value.job_id); } catch (_) {} }
    if (value?.job_id === pendingId) pendingId = '';
    if (value?.job_id === revisionPendingId) forgetRevision();
    if (value?.state !== 'available') clear();
    controls();
  }
  function citation(draft, item) {
    const details = document.createElement('details');
    details.append(element('summary', '查看依据'));
    const text = `${item.start.toFixed(3)}–${item.end.toFixed(3)} 秒；原片段 ${item.segment_indices.join(', ')}`;
    const ref = draft.source_ref;
    if (/^BV[0-9A-Za-z]{10}$/.test(ref.bvid) && Number.isInteger(ref.page) && ref.page > 0) {
      const link = element('a', text);
      link.href = `https://www.bilibili.com/video/${ref.bvid}?p=${ref.page}&t=${Math.floor(item.start)}`;
      link.target = '_blank'; link.rel = 'noopener noreferrer'; details.append(link);
    } else details.append(element('span', text));
    return details;
  }
  const itemLayout = item => item.layout === 'bullet' || item.layout === 'step' ? item.layout : 'paragraph';
  function listItem(draft, item) {
    const li = document.createElement('li');
    if (item.label) li.append(element('strong', `${item.label}：`));
    li.append(item.text, citation(draft, item));
    return li;
  }
  function renderItems(draft, items, content) {
    let index = 0;
    while (index < items.length) {
      const layout = itemLayout(items[index]);
      if (layout === 'paragraph') {
        const item = items[index++];
        if (item.label) content.append(element('h4', item.label));
        content.append(paragraph(item.text), citation(draft, item));
        continue;
      }
      let end = index + 1;
      while (end < items.length && itemLayout(items[end]) === layout) end++;
      const list = document.createElement(layout === 'step' ? 'ol' : 'ul');
      for (let i = index; i < end; i++) list.append(listItem(draft, items[i]));
      content.append(list); index = end;
    }
  }
  const heading = (id, text) => { const node = element('h3', text); node.id = id; return node; };
  function renderSections(draft, content) {
    (draft.sections ?? []).forEach((section, index) => {
      content.append(heading(`note-topic-${index + 1}`, `${index + 1}. ${section.title}`));
      renderItems(draft, section.items ?? [], content);
    });
    for (const [key, label, id] of [['terms', '核心术语', 'note-terms'], ['examples', '例子', 'note-examples']]) {
      if (!draft[key]?.length) continue;
      content.append(heading(id, label));
      renderItems(draft, draft[key], content);
    }
  }
  function renderMethods(draft, content) {
    if (!draft.methods?.length) { content.append(element('p', '服务端没有返回方法条目。')); return; }
    for (const method of draft.methods) {
      content.append(element('h3', method.title));
      if (method.applicability) content.append(paragraph(`适用：${method.applicability}`));
      if (method.steps?.length) {
        content.append(element('h4', '步骤'));
        const list = document.createElement('ol');
        for (const step of method.steps) {
          const li = document.createElement('li');
          li.append(paragraph(step.text), citation(draft, step)); list.append(li);
        }
        content.append(list);
      }
      if (method.checkpoints?.length) {
        content.append(element('h4', '检查点'));
        const list = document.createElement('ul');
        for (const checkpoint of method.checkpoints) {
          const li = document.createElement('li');
          li.append(paragraph(checkpoint.text), citation(draft, checkpoint)); list.append(li);
        }
        content.append(list);
      }
    }
  }
  const answerStatusNames = {answered: '按材料整理的回答', partial: '部分回答，仍缺材料', not_in_source: '现有材料不足以回答'};
  function renderAnswer(draft, content) {
    const status = element('p', answerStatusNames[draft.source_status] ?? '回答状态待确认，请核对下方依据与限制。');
    status.className = 'qa-status';
    const question = paragraph(draft.user_request);
    question.className = 'qa-question';
    content.append(heading('answer-question', '你的问题'), question, status);
    if (draft.answer?.length) {
      content.append(heading('answer-body', '回答'));
      for (const item of draft.answer) content.append(paragraph(item.text), citation(draft, item));
    } else content.append(element('p', '本次没有给出基于材料的回答内容。'));
    if (draft.missing?.length) {
      const box = document.createElement('div'); box.className = 'qa-missing';
      box.append(element('h3', '还缺哪些材料'));
      const list = document.createElement('ul');
      for (const item of draft.missing) list.append(element('li', item));
      box.append(list);
      content.append(box);
    }
  }
  function sourceInfo(value, name) {
    const draft = value.candidate;
    const details = document.createElement('details');
    details.append(element('summary', '来源与处理信息'));
    details.append(element('p', `视频来源：${draft.source_ref.bvid} / p=${draft.source_ref.page}`));
    details.append(element('p', `生成模型：${draft.model_id}`));
    details.append(element('p', `候选类型：${name}`));
    const skill = skillOf(value);
    if (skill) details.append(element('p', `处理技能：${skill.id} / 版本 ${skill.version} / 内容指纹 sha256 ${skill.sha256}`));
    if (kindOf(draft.kind) === 'structured_notes' && !skill) details.append(element('p', '此候选为实验资料整理稿，尚未通过笔记质量验收。'));
    if (kindOf(draft.kind) === 'source_answer') details.append(element('p', '回答与状态由模型依据给定材料作出，只是基于材料的判断，不代表现实中事实不存在，也不代表质量已通过验收。'));
    details.append(element('p', '正文来自本机语音识别转写，识别可能有误；时间点为语音活动检测片段范围，并非逐词对齐，没有读取或理解画面。'));
    details.append(element('p', '本候选不是正式任务完成凭证。'));
    return details;
  }
  function buildToc(draft, kind) {
    if (kind === 'method_list' || kind === 'source_answer') return [];
    const entries = (draft.sections ?? []).map((section, index) => ({id: `note-topic-${index + 1}`, text: `${index + 1}. ${section.title}`}));
    if (draft.terms?.length) entries.push({id: 'note-terms', text: '核心术语'});
    if (draft.examples?.length) entries.push({id: 'note-examples', text: '例子'});
    if (draft.unverified?.length) entries.push({id: 'note-limits', text: '未核实与适用范围'});
    return entries;
  }
  function renderToc(entries) {
    const list = $('notes-toc-list'); list.replaceChildren();
    for (const entry of entries) {
      const li = document.createElement('li'); const link = element('a', entry.text);
      link.href = `#${entry.id}`; li.append(link); list.append(li);
    }
    $('notes-toc').hidden = !entries.length;
  }
  function render(value) {
    result = value; const draft = value.candidate; const kind = kindOf(draft.kind);
    const name = kind === 'method_list' ? '方法清单候选' : kind === 'source_answer' ? '母版问答候选' : (skillOf(value) ? '学习笔记候选' : '实验资料整理稿');
    $('notes-title').textContent = kind === 'source_answer' ? '针对母版的回答' : (draft.title || name);
    $('notes-meta').textContent = kind === 'source_answer' ? '这份回答基于所选母版的识别文字生成，尚未核验，识别与生成都可能有误。' : `这份${name}来自本机语音识别转写，尚未核验，识别与生成可能有误。`;
    const content = $('notes-content'); content.replaceChildren();
    renderToc(buildToc(draft, kind));
    if (kind === 'method_list') renderMethods(draft, content);
    else if (kind === 'source_answer') renderAnswer(draft, content);
    else renderSections(draft, content);
    if (draft.unverified.length) {
      content.append(heading('note-limits', '未核实与适用范围'));
      for (const item of draft.unverified) content.append(paragraph(item));
    }
    content.append(sourceInfo(value, name));
    $('notes-result').hidden = false; $('notes-download-md').disabled = false; $('notes-download-json').disabled = false;
    const open = revisionOpen();
    $('notes-revision-form').hidden = !open;
    $('notes-revision-version').textContent = open ? `当前第 ${value.revision.version} 版，修改会保存为新版，原稿保留。` : '';
    $('notes-revision-instruction').value = ''; $('notes-revision-permission').checked = false;
    $('notes-revision-state').textContent = '';
    if (revisionJobId && job?.job_id === revisionJobId) { $('notes-revision-state').textContent = '新版候选已保存。'; revisionJobId = ''; }
    window.dispatchEvent(new CustomEvent('vaa-candidate-opened', {detail: {
      candidate_id: job.job_id, output_kind: kind, record_fingerprint: value.revision.record_fingerprint,
      independent_reference: value.independent_reference
    }}));
  }
  async function openHistory(row) {
    if (!connected || busy || pendingId || revisionPendingId || active(job)) return;
    const ticket = ++sequence; busy = true; clear(); controls();
    try {
      const current = await api('/app/notes/' + encodeURIComponent(row.job_id));
      const value = await api('/app/notes/' + encodeURIComponent(row.job_id) + '/candidate');
      if (ticket !== sequence || !connected) return;
      display(current); render(value); suppressCurrent = false;
      // A deduplicated catalog may expose another ID for the same frozen source.
      if (![...$('notes-source').options].some(option => option.value === row.source_id)) {
        const option = element('option', `本成果的母版 · ${row.bvid} / p=${row.page}`);
        option.value = row.source_id; $('notes-source').append(option);
      }
      $('notes-source').value = row.source_id; $('notes-permission').checked = false;
      $('notes-result').scrollIntoView({block: 'start'});
    } catch (error) { if (ticket === sequence) $('notes-error').textContent = error.message; }
    finally { busy = false; controls(); }
  }
  function historyRows(value) {
    const list = $('notes-history'); list.replaceChildren();
    for (const row of value.results) {
      const item = element('li', '');
      const version = Number.isInteger(row.version) && row.version > 0 ? ` · 第${row.version}版` : '';
      item.append(element('p', `${row.title} · ${kindNames[kindOf(row.output_kind)]}${version} · ${row.bvid} / p=${row.page}`),
        element('p', statusText(row.state, kindOf(row.output_kind))));
      if (row.state === 'available') {
        const button = element('button', '打开成果'); button.type = 'button';
        button.disabled = busy || !!pendingId || !!revisionPendingId || !!active(job);
        button.addEventListener('click', () => openHistory(row)); item.append(button);
      }
      list.append(item);
    }
    $('notes-history-hint').textContent = value.truncated ? '已显示当前读取范围内的成果。' : value.results.length ? '打开不会重新识别或调用模型；可选择原母版再生成另一种成果。' : '暂无已记录的成果。';
  }
  async function refresh() {
    if (polling || busy) return;
    polling = true; const ticket = sequence;
    try {
      const session = await api('/app/session');
      if (ticket !== sequence) return;
      if (!session.connected) {
        if (connected) { sequence++; clear(); job = null; }
        $('notes-history').replaceChildren();
        connected = false; controls(); return;
      }
      connected = true;
      const state = await api('/app/notes');
      if (ticket !== sequence) return;
      enabled = state.enabled;
      restoreRevision(state.scope_id);
      if (enabled) {
        const sourceList = await api('/app/notes/sources');
        if (ticket !== sequence) return;
        const selected = $('notes-source').value;
        $('notes-source').replaceChildren();
        for (const source of sourceList.sources) {
          const option = element('option', `${source.title} · ${source.bvid} / p=${source.page} · ${source.segments} 段`);
          option.value = source.source_id; $('notes-source').append(option);
        }
        if ([...$('notes-source').options].some(option => option.value === selected)) $('notes-source').value = selected;
        else if (selected.startsWith('artifact:') && job?.state === 'available') {
          const option = element('option', `本成果的母版 · ${job.bvid} / p=${job.page}`);
          option.value = selected; $('notes-source').append(option); $('notes-source').value = selected;
        }
        const history = await api('/app/notes/history');
        if (ticket !== sequence) return;
        historyRows(history);
      }
      let identity = pendingId || revisionPendingId || job?.job_id;
      if (!identity && !suppressCurrent) { try { identity = localStorage.getItem('vaa-notes-job'); } catch (_) {} }
      let current = suppressCurrent ? null : state.job;
      if (identity) {
        try { current = await api('/app/notes/' + encodeURIComponent(identity)); }
        catch (error) {
          if (error.status !== 404) throw error;
          if (identity === pendingId || identity === revisionPendingId) {
            $('notes-error').textContent = identity === pendingId ? '尚未确认这次提交，可再次点击刷新核对；不会自动重发生成请求。' : '尚未确认这次修改提交，可再次点击刷新核对；不会自动重新提交修改。';
            controls(); return;
          }
          try { localStorage.removeItem('vaa-notes-job'); } catch (_) {}
        }
      }
      if (ticket !== sequence) return;
      if (job?.job_id !== current?.job_id) clear();
      display(current);
      if (current?.state === 'available' && !result) {
        const value = await api(`/app/notes/${current.job_id}/candidate`);
        if (ticket === sequence && job?.job_id === current.job_id && connected) render(value);
      }
    } catch (error) {
      if (ticket !== sequence) return;
      $('notes-error').textContent = error.message;
      if ([401, 403, 404].includes(error.status)) {
        if (error.status === 401) connected = false;
        enabled = false; sequence++; clear(); job = null;
        $('notes-source').replaceChildren();
      }
    } finally { polling = false; controls(); }
  }
  $('notes-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!connected || !enabled || busy || pendingId || revisionPendingId || active(job) || !$('notes-form').reportValidity()) return;
    if (!$('notes-permission').checked) { $('notes-error').textContent = '请先明确允许将这份母版文字发送给生成模型。'; return; }
    sequence++; const ticket = sequence; clear(); job = null; suppressCurrent = false; busy = true; pendingId = crypto.randomUUID(); controls();
    $('notes-state').textContent = '正在提交整理需求…';
    try {
      const value = await api('/app/notes', {method: 'POST', body: JSON.stringify({request_id: pendingId,
        source_id: $('notes-source').value, user_request: $('notes-request').value.trim(), allow_model_text: true,
        output_kind: kindOf($('notes-output-kind').value)})});
      if (ticket === sequence && connected) display(value);
    } catch (error) {
      if (ticket !== sequence) return;
      // Definitive rejection cannot have started a job; network uncertainty keeps the ID.
      if (error.status && error.status < 500) pendingId = '';
      $('notes-error').textContent = error.message;
      if (error.status === 401) { connected = false; sequence++; clear(); }
    } finally { busy = false; controls(); }
  });
  $('notes-cancel').addEventListener('click', async () => {
    if (!job || busy) return;
    const ticket = ++sequence, identity = job.job_id; busy = true; controls();
    try { const value = await api(`/app/notes/${identity}/cancel`, {method: 'POST'}); if (ticket === sequence && connected) display(value); }
    catch (error) { $('notes-error').textContent = error.message; if (error.status === 401) { connected = false; clear(); } }
    finally { busy = false; controls(); }
  });
  $('notes-refresh').addEventListener('click', refresh);
  window.addEventListener('vaa-check-amendment', event => {
    const detail = event.detail || {};
    if (!revisionOpen() || busy || pendingId || revisionPendingId || active(job)
        || detail.candidate_id !== job.job_id
        || detail.record_fingerprint !== result.revision.record_fingerprint
        || typeof detail.instruction !== 'string' || !detail.instruction.trim()
        || detail.instruction.length > 4000) return;
    $('notes-revision-instruction').value = detail.instruction;
    $('notes-revision-permission').checked = false;
    $('notes-revision-state').textContent = '核查意见已带入修改框，请核对或编辑，再许可并提交。原稿尚未改动。';
    $('notes-revision-form').scrollIntoView({block: 'center'});
    $('notes-revision-instruction').focus();
  });
  $('notes-revision-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (!connected || !enabled || busy || pendingId || revisionPendingId || active(job) || !revisionOpen()) return;
    if (!$('notes-revision-form').reportValidity()) return;
    if (!$('notes-revision-permission').checked) { $('notes-revision-state').textContent = '请先勾选许可：允许把这份母版的识别文字与此原稿发送给生成模型。'; return; }
    revisionRequest = {parent_id: job.job_id, request_id: crypto.randomUUID(),
      expected_parent_fingerprint: result.revision.record_fingerprint,
      instruction: $('notes-revision-instruction').value.trim()};
    revisionPendingId = revisionRequest.request_id;
    try { sessionStorage.setItem(recoveryKey(), JSON.stringify(revisionRequest)); }
    catch (_) { forgetRevision(); $('notes-error').textContent = '浏览器无法暂存这次修改请求，本次未提交。请允许此页面使用本地会话存储后再试。'; controls(); return; }
    await sendRevision(revisionRequest);
  });
  async function sendRevision(request) {
    sequence++; const ticket = sequence;
    busy = true; controls();
    $('notes-revision-state').textContent = '正在提交修改请求…';
    try {
      const value = await api('/app/notes/' + encodeURIComponent(request.parent_id) + '/revisions', {method: 'POST',
        body: JSON.stringify({request_id: request.request_id, expected_parent_fingerprint: request.expected_parent_fingerprint,
          instruction: request.instruction, allow_model_text: true})});
      if (ticket !== sequence || !connected) return;
      if (value?.state === 'available') result = null;
      revisionJobId = typeof value?.job_id === 'string' ? value.job_id : '';
      display(value);
      $('notes-revision-permission').checked = false;
      $('notes-revision-state').textContent = value?.state === 'available' ? '新版候选已保存。' : '修改请求已提交；生成新版候选期间这份原稿保留不变。';
    } catch (error) {
      if (ticket !== sequence) return;
      const settled = !!error.status && error.status < 500;
      if (settled) forgetRevision();
      $('notes-error').textContent = revisionRejections[error.code] || error.message;
      $('notes-revision-state').textContent = settled ? '本次修改未提交成功，这份原稿保留不变。' : '修改提交结果待确认，保留原请求编号核对；不会自动重新提交修改。';
      if (error.status === 401) { connected = false; sequence++; clear(); }
    } finally { busy = false; controls(); }
  }
  $('notes-revision-retry-permission').addEventListener('change', controls);
  $('notes-revision-retry').addEventListener('click', async () => {
    if (!connected || !enabled || busy || pendingId || active(job) || !revisionRequest
        || !$('notes-revision-retry-permission').checked) return;
    await sendRevision(revisionRequest);
  });
  const kindHints = {source_answer: '将基于已保存的母版材料回答你在下方写下的问题；问题必填，不会重新抓取视频或读取新来源，也不生成笔记目录。'};
  function syncKindHint() {
    const kind = kindOf($('notes-output-kind').value);
    $('notes-kind-hint').textContent = kindHints[kind] || '';
    $('notes-kind-hint').hidden = !kindHints[kind];
    $('notes-request').placeholder = kind === 'source_answer' ? '例如：视频里提到的某个操作具体怎么做？请只依据材料回答' : '提取课程的概念、步骤、例子和注意事项，保留原文依据';
  }
  function changedSelection() {
    if (busy || pendingId || revisionPendingId || active(job)) return;
    sequence++; clear(); job = null; suppressCurrent = true; $('notes-permission').checked = false;
    revisionJobId = '';
    $('notes-state').textContent = '已切换选择，填写需求并明确授权后再生成。'; controls();
  }
  $('notes-output-kind').addEventListener('change', () => { syncKindHint(); changedSelection(); });
  $('notes-source').addEventListener('change', changedSelection);
  window.addEventListener('vaa-use-source', async event => {
    if (busy || pendingId || revisionPendingId || active(job)) return;
    await refresh();
    if (busy || pendingId || revisionPendingId || active(job)) return;
    if ([...$('notes-source').options].some(option => option.value === event.detail)) {
      $('notes-source').value = event.detail; changedSelection();
      $('notes-section').scrollIntoView({block: 'start'});
    }
  });
  syncKindHint();
  window.addEventListener('vaa-open-candidate', async event => {
    if (typeof event.detail !== 'string' || !/^[0-9a-f-]{36}$/i.test(event.detail)) return;
    const identity = event.detail;
    await refresh();
    await openHistory({job_id: identity, source_id: 'artifact:' + identity});
  });
  function download(extension) {
    if (!connected || !result) return;
    const kind = kindOf(result.candidate.kind);
    const prefix = kind === 'method_list' ? '方法清单候选' : kind === 'source_answer' ? '母版问答候选' : '笔记候选';
    const content = extension === 'md' ? (result.reading_markdown || result.markdown || '') : JSON.stringify(result.candidate, null, 2);
    const url = URL.createObjectURL(new Blob([content], {type: extension === 'md' ? 'text/markdown;charset=utf-8' : 'application/json'}));
    const link = document.createElement('a'); link.href = url;
    link.download = extension === 'md' && kind === 'source_answer' ? 'answer.md' : `${prefix}-${result.candidate.source_ref.bvid}-p${result.candidate.source_ref.page}.${extension}`;
    link.click(); URL.revokeObjectURL(url);
  }
  $('notes-download-md').addEventListener('click', () => download('md'));
  $('notes-download-json').addEventListener('click', () => download('json'));
  controls(); refresh(); setInterval(refresh, 3000);
})();
