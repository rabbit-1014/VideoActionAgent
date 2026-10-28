'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const scopeRe = /^[0-9a-f]{64}$/;
  const activeStates = ['queued', 'forming_requirements', 'checking'];
  const kindNames = {structured_notes: '学习笔记', method_list: '方法清单', source_answer: '母版问答'};
  const isUuid = value => typeof value === 'string' && uuid.test(value);
  const isActive = value => !!value && activeStates.includes(value.state);
  const element = (tag, text) => { const node = document.createElement(tag); node.textContent = text; return node; };
  const paragraph = text => { const node = element('p', text); node.style.whiteSpace = 'pre-line'; return node; };

  let connected = false, enabled = false, busy = false, polling = false, sequence = 0;
  let scope = '', candidateId = '', candidateKind = '', candidateFingerprint = '';
  let job = null, jobOwned = false, otherActive = false;
  let pendingRequest = null, pendingMissing = false;

  const recoveryKey = () => 'vaa-notes-check-pending-' + scope;
  const supports = () => ['structured_notes', 'source_answer'].includes(candidateKind);

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

  function clearPending() {
    pendingRequest = null; pendingMissing = false;
    if (scopeRe.test(scope)) { try { sessionStorage.removeItem(recoveryKey()); } catch (_) {} }
    $('notes-check-retry-permission').checked = false;
  }
  function storePending(request) {
    if (!scopeRe.test(scope)) return false;
    try { sessionStorage.setItem(recoveryKey(), JSON.stringify(request)); return true; }
    catch (_) { return false; }
  }
  function restorePending(nextScope) {
    const value = typeof nextScope === 'string' ? nextScope : '';
    if (value === scope) return;
    scope = value;
    pendingRequest = null; pendingMissing = false;
    $('notes-check-retry-permission').checked = false;
    if (!scopeRe.test(scope)) return;
    let stored = null;
    try { stored = JSON.parse(sessionStorage.getItem(recoveryKey()) || 'null'); } catch (_) { return; }
    if (!stored || !isUuid(stored.request_id) || !isUuid(stored.candidate_id)
        || stored.allow_model_text !== true) {
      try { sessionStorage.removeItem(recoveryKey()); } catch (_) {}
      return;
    }
    pendingRequest = {request_id: stored.request_id, candidate_id: stored.candidate_id, allow_model_text: true};
  }

  function clearFindings() { $('notes-check-findings').replaceChildren(); }
  function resetForCandidate(id, kind, fingerprint) {
    sequence++;
    candidateId = id; candidateKind = kind; candidateFingerprint = fingerprint || '';
    job = null; jobOwned = false; otherActive = false;
    clearFindings();
    $('notes-check-error').textContent = '';
    $('notes-check-permission').checked = false;
    $('notes-check-retry-permission').checked = false;
    pendingMissing = false;
  }
  function onUnauthorized() {
    connected = false; enabled = false; sequence++;
    job = null; jobOwned = false; otherActive = false;
    // Authentication loss cannot settle an earlier unknown POST. Hide its identity,
    // retain the frozen request only in its original scope, and query after reconnect.
    pendingRequest = null; pendingMissing = false; scope = '';
    $('notes-check-retry-permission').checked = false;
    clearFindings();
    $('notes-check-error').textContent = '';
    $('notes-check-permission').checked = false;
    controls();
  }

  function controls() {
    const open = connected && !!candidateId;
    $('notes-check-section').hidden = !open;
    const locked = busy || !!pendingRequest || isActive(job) || otherActive;
    $('notes-check-permission').disabled = locked;
    $('notes-check-start').disabled = !connected || !enabled || !supports() || locked
      || !$('notes-check-permission').checked;
    $('notes-check-refresh').disabled = !connected || busy || !candidateId;
    $('notes-check-cancel').disabled = !connected || busy || !jobOwned || !isActive(job) || !isUuid(job?.job_id);
    const sameCandidate = !!pendingRequest && pendingRequest.candidate_id === candidateId;
    $('notes-check-retry').hidden = !(connected && enabled && pendingRequest);
    $('notes-check-retry-text').textContent = !pendingRequest ? ''
      : sameCandidate ? '原核对编号：' + pendingRequest.request_id
      : '上一份成果的原要求核查请求尚未确认；请重新打开对应成果后再核对，这里不会把它的编号或结果显示在当前成果上。';
    $('notes-check-retry-submit').disabled = !connected || !enabled || busy || !sameCandidate
      || !pendingMissing || !$('notes-check-retry-permission').checked;
  }

  const jobStates = {
    queued: '核查已接收，等待开始。',
    forming_requirements: '正在从原要求与逐次修改整理冻结要求（此阶段不接触本稿）。',
    checking: '正在把此稿与母版转写交给配置核查模型逐项核对，不会改动本稿。',
    available: '核查意见已保存，见下方。',
    canceled: '已取消核查；已发生的模型调用无法撤销。',
    interrupted: '上次核查结果未确认，未自动重试；可刷新核对。',
    failed: '本次核查失败。'
  };
  const errorNames = {
    model_text_permission_required: '服务未收到本次核查的模型使用授权',
    model_unavailable: '配置的核查模型当前不可用',
    requirements_failed: '未能从原要求整理出可核对的要求',
    candidate_unavailable: '这份成果当前无法读取',
    candidate_changed: '这份成果的内容已变化，无法核对',
    invalid_model_result: '模型意见未符合服务校验要求',
    check_in_progress: '已有另一项核查正在进行'
  };
  const errorText = code => errorNames[code] || (typeof code === 'string' && code ? '原因类型 ' + code : '原因未说明');

  function display() {
    const open = connected && !!candidateId;
    $('notes-check-section').hidden = !open;
    if (!open) { controls(); return; }
    let state = '';
    if (!enabled) state = '本机原要求核查未开启；这份成果仍可正常阅读与下载。';
    else if (!supports()) state = (kindNames[candidateKind] || '当前成果') + '暂不支持按原要求核对；本功能适用于学习笔记和母版问答候选。';
    else if (pendingRequest && pendingRequest.candidate_id === candidateId && !pendingMissing)
      state = '正在确认这次核查提交（原请求编号）；刷新只做查询，不会自动重复提交。';
    else if (pendingRequest && pendingRequest.candidate_id === candidateId && pendingMissing)
      state = '这次核查提交在服务中未找到记录；可用原编号重新许可后显式重试。';
    else if (pendingRequest)
      state = '上一份成果的原要求核查请求尚未确认；请先重新打开对应成果核对，本稿暂不另发核查。';
    else if (otherActive)
      state = '已有另一份成果的核查正在进行；请等待其完成或取消后，再对本稿发起核查。';
    else if (jobOwned && job) state = jobStates[job.state] || '核查状态待确认。';
    else state = '可核对这份成果候选的当前版本是否覆盖原要求；结果只是模型意见，不是验收结论。';
    $('notes-check-state').textContent = state;
    $('notes-check-error').textContent = jobOwned && job?.state === 'failed'
      ? '核查未能完成：' + errorText(job.error_code) + '。未发布意见，不会自动重试。' : '';
    controls();
  }

  const findingStatus = {
    unmet: {label: '存在未满足要求', section: '存在未满足要求'},
    uncertain: {label: '仍有待核实要求', section: '仍有待核实要求'},
    met: {label: '模型认为已有覆盖，仍待核对', section: '模型认为已有覆盖（仍待核对）'}
  };
  const outcomeText = {
    all_reported_met: '核查模型未发现遗漏，仍待核对。',
    unmet: '存在未满足要求。',
    uncertain: '仍有待核实要求。'
  };
  function requirementMap(value) {
    const map = new Map();
    const list = value?.requirements?.requirements;
    if (Array.isArray(list)) for (const item of list) if (item && typeof item.id === 'string') map.set(item.id, item);
    return map;
  }
  function appendIndices(container, list) {
    if (!Array.isArray(list)) return;
    const nums = list.filter(value => Number.isInteger(value));
    if (nums.length) container.append(element('p', '原片段：' + nums.join(', ')));
  }
  function appendSupport(container, support) {
    if (typeof support === 'string') {
      const labels = {supported:'模型认为来源支持',missing:'模型认为材料未给出',uncertain:'模型认为来源仍不确定',not_applicable:'模型未对本项判断来源支持'};
      if (support) container.append(paragraph('来源判断（模型意见）：' + (labels[support] || support))); return;
    }
    if (!Array.isArray(support)) return;
    const items = support.filter(Boolean);
    if (!items.length) return;
    const list = document.createElement('ul');
    for (const item of items) {
      const li = document.createElement('li');
      if (typeof item === 'string') li.textContent = item;
      else if (item && typeof item === 'object') {
        const text = [item.text, item.excerpt, item.quote, item.support].find(value => typeof value === 'string' && value);
        li.textContent = text || JSON.stringify(item);
      } else li.textContent = String(item);
      list.append(li);
    }
    container.append(element('p', '来源支持：'), list);
  }
  const roleNames = {
    transcript_match: '与保存转写逐字对应；录音准确性仍未核实',
    unverified_interpretation: '模型声称原文，但未逐字对应；作为待核解释保留',
    paraphrase_unverified: '模型改写；是否忠实表达材料仍待核实',
    author_inference: '作者推断，不能当作视频原话',
    uncertain: '文字角色尚不确定'
  };
  const coverageNames = {provided: '已绑定正文内容，绑定仍是模型意见',
    missing: '未绑定到有效正文内容', uncertain: '尚未确认对应章节或内容'};
  const modelExplanation = finding => typeof finding?.explanation === 'string'
    ? finding.explanation.split('〔程序〕')[0] : '';
  const roleIssues = finding => (finding.role_details || []).flatMap(item => item.clauses || [])
    .filter(clause => clause.effective_role === 'unverified_interpretation'
      || (clause.effective_role === 'author_inference' && !clause.disclosed));
  function amendmentFindings(evaluation) {
    const list = Array.isArray(evaluation?.findings) ? evaluation.findings : [];
    if (evaluation.status !== 'method_role_model_opinion') return list.filter(item => item.status !== 'met');
    // A paraphrase awaiting semantic review is not by itself a demand to rewrite it.
    return list.filter(item => item.coverage !== 'provided' || roleIssues(item).length);
  }
  function appendRoles(container, details) {
    if (!Array.isArray(details) || !details.length) return;
    const list = document.createElement('ul');
    for (const item of details) {
      if (!Array.isArray(item.clauses)) continue;
      for (const clause of item.clauses) {
        const li = document.createElement('li');
        li.append(paragraph(clause.text), paragraph('文字角色：' + (roleNames[clause.effective_role] || '待核实')));
        if (clause.effective_role === 'author_inference') {
          li.append(paragraph(clause.disclosed ? '正文已标明推断。' : '正文未标明推断，建议标明或核对。'));
        }
        if (Array.isArray(clause.source_quotes)) for (const quote of clause.source_quotes) {
          li.append(paragraph('保存转写原话（片段 ' + quote.index + '）：' + quote.text));
        }
        list.append(li);
      }
    }
    container.append(list);
  }
  function renderFindingList(findings, status, requirements, methodReview) {
    const list = document.createElement('ul');
    list.className = 'check-findings';
    for (const finding of findings) {
      if (!finding || typeof finding !== 'object') continue;
      const li = document.createElement('li');
      const requirement = typeof finding.requirement_id === 'string' ? requirements.get(finding.requirement_id) : null;
      const requirementText = requirement && typeof requirement.text === 'string' && requirement.text
        ? requirement.text : '原要求内容未提供，请刷新核对。';
      li.append(element('p', (methodReview ? '明示分项要求：' : '模型整理的要求：') + requirementText));
      const label = findingStatus[status]?.label
        || (typeof finding.status === 'string' && finding.status ? '状态：' + finding.status : '状态待确认');
      const statusNode = element('p', '判断：' + label);
      statusNode.className = 'check-status check-status-' + status;
      li.append(statusNode);
      if (methodReview) {
        li.append(paragraph('内容对应：' + (coverageNames[finding.coverage] || '待核实')));
        li.append(paragraph('绑定解释（模型意见）：' + modelExplanation(finding)));
      } else if (typeof finding.explanation === 'string' && finding.explanation) li.append(paragraph('解释：' + finding.explanation));
      const details = document.createElement('details');
      details.append(element('summary', '展开原话与来源片段'));
      const body = document.createElement('div');
      if (typeof finding.candidate_excerpt === 'string' && finding.candidate_excerpt) {
        body.append(element('p', '本稿原话：'));
        body.append(paragraph(finding.candidate_excerpt));
      }
      appendIndices(body, finding.source_indices);
      if (methodReview) appendRoles(body, finding.role_details);
      else appendSupport(body, finding.source_support);
      if (!body.childNodes.length) body.append(element('p', '本次意见没有附带可展开的原话或片段。'));
      details.append(body);
      li.append(details);
      list.append(li);
    }
    return list;
  }
  function renderRequirements(list) {
    const details = document.createElement('details');
    details.append(element('summary', '查看本次核对使用的冻结要求（' + list.length + ' 项）'));
    const ul = document.createElement('ul');
    for (const requirement of list) {
      if (!requirement || typeof requirement !== 'object') continue;
      const li = document.createElement('li');
      li.append(element('p', typeof requirement.text === 'string' && requirement.text ? requirement.text : '要求内容未提供'));
      if (typeof requirement.kind === 'string' && requirement.kind) li.append(element('p', '类型：' + requirement.kind));
      if (Array.isArray(requirement.basis)) for (const basis of requirement.basis) {
        if (typeof basis.quote === 'string') li.append(paragraph((basis.version === 0 ? '原目标原话：' : '第' + basis.version + '版修改原话：') + basis.quote));
      }
      ul.append(li);
    }
    details.append(ul);
    return details;
  }
  function renderResult(value) {
    clearFindings();
    if (!value || value.candidate_id !== candidateId) return;
    const requirements = requirementMap(value);
    const groups = {unmet: [], uncertain: [], met: [], other: []};
    const findings = Array.isArray(value.evaluation?.findings) ? value.evaluation.findings : [];
    for (const finding of findings) {
      const status = finding && typeof finding.status === 'string' && Object.hasOwn(findingStatus, finding.status) ? finding.status : 'other';
      groups[status].push(finding);
    }
    const outcome = typeof value.evaluation?.outcome === 'string' ? value.evaluation.outcome : '';
    const methodReview = value.requirements?.schema_version === 2
      && value.requirements?.status === 'declared_method_fields'
      && value.evaluation?.status === 'method_role_model_opinion';
    const container = $('notes-check-findings');
    container.append(paragraph('总体意见：' + (outcomeText[outcome] || '核查意见已返回，具体结论见下方各项，仍待你核对。')));
    container.append(paragraph(methodReview
      ? '本次从你的明示结构固定每种方法的分项要求。只核这些分项与文字角色，其他要求尚未判断；内容绑定与改写、推断分类仍是模型意见，可能出错。逐字对应只证明与保存转写相符，不证明录音或现行事实准确，也不是质量验收或任务完成。'
      : '以下为模型对“原要求是否被覆盖”的判断，模型可能理解或判断错误；这不是验收结论，也不代表质量合格或任务完成。'));
    const original = document.createElement('details');
    original.append(element('summary', '原始目标与历次修改（原文）'));
    if (typeof value.requirements?.user_request === 'string') original.append(paragraph('原始目标：' + value.requirements.user_request));
    if (Array.isArray(value.requirements?.amendments)) for (const amendment of value.requirements.amendments) {
      original.append(paragraph('第' + amendment.version + '版修改：' + amendment.instruction));
    }
    container.append(original);
    if (methodReview) {
      for (const finding of findings) for (const clause of roleIssues(finding)) {
        const name = requirements.get(finding.requirement_id)?.text || '正文';
        container.append(paragraph(name + '：' + (clause.effective_role === 'author_inference'
          ? '有未标明的推断' : '有尚未与转写逐字对应的解释') + '「' + clause.text
          + '」。可标明或核对，不能当作视频原话。'));
      }
    }
    if (!findings.length) container.append(paragraph('本次没有返回逐项意见；请刷新核对或视为待核实。'));
    for (const key of ['unmet', 'uncertain', 'met', 'other']) {
      if (!groups[key].length) continue;
      if (methodReview && key === 'uncertain') {
        const details = document.createElement('details');
        details.append(element('summary', groups[key].length + ' 个分项仍需核对（查看内容与文字角色）'));
        details.append(renderFindingList(groups[key], key, requirements, true));
        container.append(details);
      } else {
        container.append(element('h3', key === 'other' ? '其他意见' : findingStatus[key].section));
        container.append(renderFindingList(groups[key], key, requirements, methodReview));
      }
    }
    if (Array.isArray(value.requirements?.requirements) && value.requirements.requirements.length) {
      container.append(renderRequirements(value.requirements.requirements));
    }
    if (methodReview && Array.isArray(value.evaluation.reviewed_items)) {
      const roles = document.createElement('details');
      roles.append(element('summary', '查看已绑定方法全部正文的文字角色'));
      appendRoles(roles, value.evaluation.reviewed_items);
      container.append(roles);
    }
  }

  async function refresh() {
    if (polling || busy) return;
    polling = true; const ticket = sequence;
    try {
      const session = await api('/app/session');
      if (ticket !== sequence) return;
      if (!session.connected) { onUnauthorized(); display(); return; }
      connected = true;
      let state;
      try { state = await api('/app/notes-checks' + (candidateId ? '?candidate_id=' + encodeURIComponent(candidateId) : '')); }
      catch (error) {
        if (error.status !== 404) throw error;
        enabled = false; job = null; jobOwned = false; otherActive = false; clearFindings();
        display(); return;
      }
      if (ticket !== sequence) return;
      enabled = state.enabled === true;
      restorePending(state.scope_id);
      if (!enabled) {
        job = null; jobOwned = false; otherActive = false; clearFindings();
        display(); return;
      }
      const globalJob = state.job && isUuid(state.job.job_id) ? state.job : null;
      let resolved = null;
      if (pendingRequest) {
        let found = null, missing = false;
        try { found = await api('/app/notes-checks/' + encodeURIComponent(pendingRequest.request_id)); }
        catch (error) { if (error.status === 404) missing = true; else throw error; }
        if (ticket !== sequence) return;
        if (missing) pendingMissing = true;
        else if (found) { resolved = found; clearPending(); }
      }
      job = null; jobOwned = false; otherActive = false;
      const tracked = resolved || globalJob;
      if (tracked) {
        if (tracked.candidate_id === candidateId) { job = tracked; jobOwned = true; }
        else if (isActive(tracked)) { job = tracked; otherActive = true; }
      }
      if (jobOwned && job.state === 'available' && job.result_available === true) {
        const value = await api('/app/notes-checks/' + encodeURIComponent(job.job_id) + '/result');
        if (ticket !== sequence) return;
        if (value.candidate_fingerprint !== candidateFingerprint) throw new Error('Candidate binding changed');
        renderResult(value);
        const missing = amendmentFindings(value.evaluation);
        if (missing.length) {
          const methodReview = value.evaluation.status === 'method_role_model_opinion';
          const button = element('button', methodReview ? '将缺项与文字角色问题带入修改' : '将未满足或待核实项带入修改');
          button.type = 'button';
          button.addEventListener('click', () => {
            if (value.candidate_id !== candidateId || value.candidate_fingerprint !== candidateFingerprint) return;
            const requirements = requirementMap(value);
            const lines = missing.map(item => '- ' + (requirements.get(item.requirement_id)?.text || '')
              + '：' + (methodReview ? (item.coverage !== 'provided'
                ? (coverageNames[item.coverage] || '待核实') + '；' + modelExplanation(item)
                : roleIssues(item).map(clause => '请标明或核对「' + clause.text + '」，不能当作视频原话').join('；')) : item.explanation));
            const instruction = ('按原要求修改这份稿件。核查模型给出以下意见，请逐项核对；意见可能有误。材料不足的内容明确列为缺口或待核实，不能补造事实。\n' + lines.join('\n')).slice(0, 4000);
            window.dispatchEvent(new CustomEvent('vaa-check-amendment', {detail: {
              candidate_id: candidateId, record_fingerprint: candidateFingerprint, instruction
            }}));
          });
          $('notes-check-findings').append(button, paragraph('此操作只填入修改框；你可以编辑，重新许可并提交后才会生成新版。'));
        }
      } else if (!jobOwned || job.state !== 'available') clearFindings();
      display();
    } catch (error) {
      if (ticket !== sequence) return;
      if (error.status === 401) { onUnauthorized(); display(); return; }
      clearFindings();
      display();
      $('notes-check-error').textContent = '核查状态暂时无法确认，请稍后点击“查询核对进度”；不会自动重试提交。';
    } finally { polling = false; controls(); }
  }

  async function sendCheck(request) {
    const ticket = ++sequence; busy = true; clearFindings(); controls();
    let message = '', settled = false;
    $('notes-check-state').textContent = '正在提交原要求核查请求…';
    try {
      const value = await api('/app/notes/' + encodeURIComponent(request.candidate_id) + '/checks',
        {method: 'POST', body: JSON.stringify({request_id: request.request_id, allow_model_text: true})});
      if (ticket !== sequence) return;
      clearPending();
      if (value && value.candidate_id === candidateId) { job = value; jobOwned = true; otherActive = false; }
      $('notes-check-permission').checked = false;
    } catch (error) {
      if (ticket !== sequence) return;
      settled = !!error.status && error.status < 500;
      if (settled) clearPending();
      if (error.status === 401) { onUnauthorized(); return; }
      message = settled
        ? '核查请求被拒绝：' + errorText(error.code) + '。可核对后重新发起。'
        : '提交结果待确认：已保留原请求编号，刷新或“查询核对进度”会按原编号核对；不会自动重复提交。';
    } finally {
      busy = false;
      if (connected) { display(); if (message) $('notes-check-error').textContent = message; }
      controls();
      if (connected) refresh();
    }
  }

  window.addEventListener('vaa-candidate-opened', event => {
    const detail = event.detail || {};
    if (!isUuid(detail.candidate_id)) return;
    const kind = typeof detail.output_kind === 'string' && detail.output_kind ? detail.output_kind : 'structured_notes';
    if (detail.candidate_id === candidateId && kind === candidateKind) { refresh(); return; }
    resetForCandidate(detail.candidate_id, kind, detail.record_fingerprint);
    refresh();
  });
  window.addEventListener('vaa-candidate-cleared', () => {
    sequence++;
    candidateId = ''; candidateKind = ''; candidateFingerprint = '';
    job = null; jobOwned = false; otherActive = false;
    clearFindings();
    $('notes-check-error').textContent = '';
    $('notes-check-permission').checked = false;
    $('notes-check-retry-permission').checked = false;
    pendingMissing = false;
    controls();
  });

  $('notes-check-start').addEventListener('click', async () => {
    if (!connected || !enabled || !supports() || busy || pendingRequest || isActive(job)) return;
    if (!$('notes-check-permission').checked) {
      $('notes-check-error').textContent = '请先勾选许可：允许把这份成果的原要求与逐次修改、当前成果及母版转写文字发送给配置核查模型。';
      return;
    }
    if (!scopeRe.test(scope)) {
      $('notes-check-error').textContent = '当前会话范围尚未确认，暂时无法安全暂存这次核查请求，未发出任何请求。请稍后刷新再试。';
      return;
    }
    const request = {request_id: crypto.randomUUID(), candidate_id: candidateId, allow_model_text: true};
    if (!storePending(request)) {
      $('notes-check-error').textContent = '浏览器无法暂存这次核查请求，本次未提交，没有发出任何请求。请允许此页面使用本地会话存储后再试。';
      return;
    }
    pendingRequest = request; pendingMissing = false;
    $('notes-check-error').textContent = '';
    await sendCheck(request);
  });
  $('notes-check-retry-submit').addEventListener('click', async () => {
    if (!connected || !enabled || busy || !pendingRequest || !pendingMissing
        || pendingRequest.candidate_id !== candidateId || !$('notes-check-retry-permission').checked) return;
    await sendCheck(pendingRequest);
  });
  $('notes-check-refresh').addEventListener('click', refresh);
  $('notes-check-cancel').addEventListener('click', async () => {
    if (!connected || busy || !jobOwned || !job || !isActive(job) || !isUuid(job.job_id)) return;
    const ticket = ++sequence, identity = job.job_id; busy = true; controls();
    let message = '';
    try {
      const value = await api('/app/notes-checks/' + encodeURIComponent(identity) + '/cancel', {method: 'POST'});
      if (ticket !== sequence) return;
      if (value && value.candidate_id === candidateId) { job = value; jobOwned = true; }
    } catch (error) {
      if (ticket !== sequence) return;
      if (error.status === 401) { onUnauthorized(); return; }
      message = '取消结果待确认，请点击“查询核对进度”核对；已发生的模型调用无法撤销，也不会撤销已完成的意见。';
    } finally {
      busy = false;
      if (connected) { display(); if (message) $('notes-check-error').textContent = message; }
      controls();
    }
  });
  $('notes-check-permission').addEventListener('change', controls);
  $('notes-check-retry-permission').addEventListener('change', controls);

  controls();
  display();
  // Saved opinions are immutable. Do not rebuild open details while the user reads.
  setInterval(() => {
    if (isActive(job) || otherActive || (pendingRequest && !pendingMissing)) refresh();
  }, 3000);
})();
