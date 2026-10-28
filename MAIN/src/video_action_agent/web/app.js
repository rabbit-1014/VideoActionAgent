'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
  if (location.hash) history.replaceState(null, '', location.pathname + location.search);
  let taskId = '', task = null, artifact = null, generation = 0, readSequence = 0, busy = false, sessionConnected = false;
  let capabilities = [], executionEnabled = false, observation = null;
  let sourceCandidate = null, sessionGeneration = 0;
  let captureEnabled = false, intakeJob = null, intakeRequestId = '', intakeBusy = false, intakeSequence = 0, sourceSequence = 0, sourceJobId = '';
  let selectedIntakeId = '';
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const say = text => { $('notice').textContent = text; };
  const node = (tag, text) => { const el = document.createElement(tag); el.textContent = text ?? ''; return el; };
  function clearSource() {
    sourceCandidate = null; $('source-preview').hidden = true;
    for (const id of ['source-preview-title', 'source-preview-meta', 'source-preview-acquisition', 'source-preview-time']) $(id).textContent = '';
    $('source-segments').replaceChildren(); $('source-segments-details').open = false;
    $('source-download-md').disabled = true; $('source-download-srt').disabled = true;
    $('source-use').disabled = true;
  }
  function connected() { if (!sessionConnected) { sessionGeneration++; sourceSequence++; intakeSequence++; clearSource(); intakeJob = null; sourceJobId = ''; $('intake-state').textContent = ''; } $('connection').hidden = sessionConnected; controls(); }
  function modeAvailable() { return executionEnabled && capabilities.includes('method_list_' + $('source-mode').value); }
  function renderMode() {
    const audio = $('source-mode').value === 'audio_transcription';
    $('permission-label').textContent = audio ? '允许本机助手下载此链接指定分P的音轨并在本机进行语音识别；音频不上传，识别文字交给生成模型整理这次方法清单' : '允许本机助手读取此视频的平台字幕，用于这次方法清单';
    $('mode-help').textContent = audio ? '音轨识别不需要 Chrome 登录。识别可能需要数分钟，请保留包含分P参数的原视频链接；识别文字可能有误。' : '请在本机 Chrome 中保持 B站登录和浏览器连接可用。字幕读取失败不会自动改用音轨。';
    $('mode-availability').textContent = modeAvailable() ? '此读取方式已开放。' : '当前未开放此读取方式，可查看已有任务。需启用本机对应处理能力后再提交或修改。';
    controls();
  }
  function controls() {
    $('source-use').disabled = !sessionConnected || !sourceCandidate || !uuid.test(sourceJobId);
    $('submit').disabled = busy || !sessionConnected || !modeAvailable();
    $('revise').disabled = busy || !task || !sessionConnected || !modeAvailable();
    $('cancel').disabled = busy || !task || task.contract_state === 'canceled' || task.completed;
    $('accept').disabled = busy || !task || !artifact || task.completed || task.contract_state === 'canceled';
    $('download').disabled = !artifact || typeof artifact.markdown !== 'string';
    const active = intakeJob && ['queued', 'running', 'cancel_requested'].includes(intakeJob.state);
    $('intake-submit').disabled = !sessionConnected || !captureEnabled || intakeBusy || !!active || !!intakeRequestId;
    $('intake-cancel').disabled = !sessionConnected || intakeBusy || !intakeJob || !['queued', 'running'].includes(intakeJob.state);
    $('intake-refresh').disabled = !sessionConnected || intakeBusy;
    $('intake-availability').textContent = captureEnabled ? '本机内容提取已开放。' : '本机内容提取未开启；已保存来源仍可查看。';
  }
  async function api(path, options = {}, missingOK = false) {
    const headers = { ...(token ? {Authorization: 'Bearer ' + token} : {}), ...options.headers };
    if (options.body) headers['Content-Type'] = 'application/json';
    const response = await fetch(path, { ...options, headers, cache: 'no-store' });
    if (missingOK && response.status === 404) return null;
    if (!response.ok) { const error = new Error(response.status === 401 ? '连接凭证无效，请重新连接。' : response.status === 409 ? '任务已发生变化。已刷新，请检查最新状态后再次操作。' : response.status === 404 ? '未找到当前任务或成果，请核对任务编号与访问凭证。' : '操作未成功，请稍后刷新重试。'); error.status = response.status; throw error; }
    return response.json();
  }
  function input() {
    if (!$('request-form').reportValidity()) throw new Error('请填写视频链接和需求。');
    if (!modeAvailable()) throw new Error('当前未开放所选读取方式，请启用对应本机处理能力后再提交。');
    if (!$('permission').checked) throw new Error('请先勾选所选方式的来源读取许可。');
    const body = {message: $('message').value.trim(), source: $('source').value.trim(), output_kind: 'method_list'};
    return $('source-mode').value === 'audio_transcription' ? {...body, source_mode: 'audio_transcription', local_source_actions: ['audio_transcription']} : {...body, local_browser_actions: ['subtitle']};
  }
  function select(id) {
    taskId = id; task = null; artifact = null; observation = null; generation++; readSequence++;
    $('task-id').value = id; $('task-panel').hidden = true; $('candidate-panel').hidden = true;
    try { localStorage.setItem('video-action-task-id', id); } catch (_) { /* ID persistence is optional. */ }
    controls();
  }
  function stateLabel(value) {
    if (value.contract_state === 'canceled') return '已取消';
    if (value.completed === true) return '已完成（已由你验收）';
    if (databaseFailure(value)) return databaseFailure(value) === 'failed' ? '处理失败，请检查当前任务提示' : '处理结果待确认';
    if (artifact) return '候选已可查看，等待你检查与验收';
    if (observation?.state === 'waiting_for_browser') return '等待浏览器连接';
    if (observation?.state === 'retryable_failure') return '本次处理失败，等待重试';
    if (observation?.state === 'waiting_for_local_asr') return '等待本机语音识别';
    if (observation?.state === 'requires_source_reconciliation') return '来源状态待核对';
    if (observation?.state === 'dispatch_refused') return '当前处理未能启动';
    if (observation?.state === 'failed') return '本次处理失败';
    if (observation?.state === 'outcome_unknown') return '本次处理结果待确认';
    if (value.formation?.waiting) return '等待必要信息或条件';
    if (value.contract_state === 'unformable') return '当前需求暂时无法形成任务';
    if (value.work_current && ['leased', 'running'].includes(value.work_state)) return '正在处理';
    if (['forming', 'accepted', 'reading', 'building', 'reviewing'].includes(value.contract_state) || value.work_state === 'ready') return '已接收，等待处理';
    return '待确认';
  }
  function databaseFailure(value) {
    const goals = value.formation?.required_goals || [];
    if (value.contract_state === 'failed' || value.last_work_outcome === 'failed' || goals.some(goal => goal.state === 'failed')) return 'failed';
    if (value.contract_state === 'unknown' || value.last_work_outcome === 'unknown' || goals.some(goal => goal.state === 'unknown')) return 'unknown';
    return null;
  }
  function localTime(value) {
    if (typeof value !== 'string') return null;
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? date.toLocaleString('zh-CN', {hour12: false}) : null;
  }
  function renderProcessing() {
    const failure = databaseFailure(task);
    const visible = observation && !task.completed && task.contract_state !== 'canceled' && !databaseFailure(task);
    $('processing-message').textContent = task.completed || task.contract_state === 'canceled' ? '当前任务已结束处理，以任务状态为准。' : failure === 'failed' ? '当前任务记录为处理失败，这不等于视频没有内容。可检查来源范围后修改需求，或取消任务。' : failure === 'unknown' ? '当前任务记录为结果未知，需要先核对已有操作；不会据此显示完成。' : visible ? observation.message : '暂无当前任务的本次处理观察，具体状态以任务与候选为准。';
    const observedAt = visible ? localTime(observation.observed_at) : null;
    const retryAt = visible ? localTime(observation.retry_at) : null;
    $('processing-time').textContent = [observedAt ? '本机本次运行最近观察：' + observedAt : '', retryAt ? '下次可重试时间：' + retryAt : ''].filter(Boolean).join('；');
    $('processing-time').hidden = !observedAt && !retryAt;
  }
  function renderTask() {
    $('task-panel').hidden = false; $('current-id').textContent = '任务编号：' + taskId;
    $('task-state').textContent = stateLabel(task);
    const stages = { form_goal: '正在整理任务目标', read_source: '正在读取指定来源', build_artifact: '正在整理方法清单', review_artifact: '正在生成审查意见' };
    $('task-detail').textContent = task.formation?.blocker?.question || task.formation?.blocker?.waiting_condition || (task.work_current && ['leased', 'running'].includes(task.work_state) ? stages[task.work_kind] || '本机正在处理当前任务' : '页面每 5 秒刷新当前状态。修改任务后，旧版本候选不再作为当前成果显示。');
    $('goals').replaceChildren();
    for (const goal of task.formation?.required_goals || []) $('goals').append(node('p', goal.title));
    renderProcessing();
    controls();
  }
  function evidence(item, ref) {
    const seconds = item.start;
    if (!ref || !/^BV[0-9A-Za-z]{10}$/.test(ref.bvid) || !Number.isInteger(ref.page) || ref.page < 1 || typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return null;
    const url = new URL('https://www.bilibili.com/video/' + ref.bvid); url.searchParams.set('p', String(ref.page)); url.searchParams.set('t', String(Math.floor(seconds)));
    const a = node('a', '查看 ' + Math.floor(seconds / 60) + ':' + String(Math.floor(seconds % 60)).padStart(2, '0') + ' 的来源'); a.href = url.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
  }
  function segmentTime(seconds) {
    return typeof seconds === 'number' && Number.isFinite(seconds) && seconds >= 0 ? Math.floor(seconds / 60) + ':' + (Math.floor(seconds % 60 * 1000) / 1000).toFixed(3).padStart(6, '0') : '时间待确认';
  }
  function renderSource(currentCapture = false) {
    const value = sourceCandidate, ref = value.source_ref, segments = value.segments;
    $('source-segments').replaceChildren();
    $('source-preview-title').textContent = ref.part_title || '已保存的音轨转写';
    $('source-preview-meta').textContent = ref.bvid + ' · 第 ' + ref.page + ' 分P · CID ' + ref.cid + ' · ' + segments.length + ' 段 · 音轨时长 ' + segmentTime(value.duration);
    $('source-preview-acquisition').textContent = currentCapture ? '这是本次获许可的来源读取与本机转写结果，来源时间以实际观察记录为准。查看和下载不会再次读取音轨。' : value.acquisition === 'recorded_audio' ? '历史音轨来源候选复用；本轮没有在线读取视频或音轨。' : '音轨是在原来源观察时间经匿名公开接口取得；打开此页面没有重新读取视频或音轨。';
    $('source-preview-time').textContent = '原来源观察时间：' + (localTime(value.source_observed_at) || '未提供') + '；本次服务载入时间：' + (localTime(value.loaded_at) || '未提供');
    for (const segment of segments) {
      const li = node('li', ''); li.append(node('span', segmentTime(segment.start) + '–' + segmentTime(segment.end)), node('p', segment.text));
      const link = evidence(segment, ref); if (link) li.append(link);
      $('source-segments').append(li);
    }
    $('source-download-md').disabled = typeof value.markdown !== 'string';
    $('source-download-srt').disabled = typeof value.srt !== 'string';
    $('source-preview').hidden = false;
  }
  async function loadSource(guard) {
    const sequence = ++sourceSequence;
    try {
      const value = await api('/app/source-candidate', {signal: AbortSignal.timeout(10000)}, true);
      if (guard !== sessionGeneration || sequence !== sourceSequence || !sessionConnected || !value) return;
      if (value.state !== 'unverified_source_candidate' || value.formal_task !== false || value.completed !== false || !['recorded_audio', 'live_anonymous_audio'].includes(value.acquisition) || value.source_ref?.kind !== 'audio_transcription' || !Array.isArray(value.segments)) throw new Error();
      sourceCandidate = value; renderSource();
    } catch (error) {
      if (guard !== sessionGeneration || sequence !== sourceSequence) return;
      clearSource();
      if (error.status === 401) { sessionConnected = false; connected(); }
      say(error.status === 401 ? '连接已失效，请重新连接。' : '已存来源暂时无法读取，可刷新页面重新连接后重试；已有任务仍可查看。');
    }
  }
  function renderIntake() {
    const stages = {resolving: '正在核对视频与分P', downloading: '正在下载指定分P音轨', transcribing: '正在本机识别语音', saving: '正在保存来源候选'};
    const errors = {no_speech_recognized: '本次未识别到语音，不证明视频没有内容', empty_audio: '本次取得的音频没有可识别数据，不证明视频没有内容', timeout: '本次读取超时，未成功取得来源候选', transcription_timeout: '本机识别超时，未成功取得来源候选', worker_unavailable: '本机处理进程暂不可用', worker_closed: '本机处理进程已关闭', local_runtime_unavailable: '本机识别环境不可用', source_unavailable: '本次来源暂不可读取', source_mismatch: '来源身份与指定分P不匹配', duration_limit: '指定音轨时长超过当前处理范围', decode_failed: '本次音频解码失败', transcription_failed: '本次语音识别失败', invalid_worker_result: '本次识别结果未通过校验', source_bundle_unavailable: '本次来源候选未能安全保存', intake_failed: '本次来源读取失败'};
    const states = {queued: '已接收，等待内容提取', running: Object.hasOwn(stages, intakeJob?.stage) ? stages[intakeJob.stage] : '正在提取内容', cancel_requested: '已请求取消，等待本机处理停止；已发生的读取无法撤销', canceled: '已取消，不公开本次结果；取消前可能已经读取音轨', available: '来源候选已可查看与下载，尚未核实', interrupted: '上次运行结果未确认，未自动重试', failed: (Object.hasOwn(errors, intakeJob?.error_code) ? errors[intakeJob.error_code] : '本次来源读取失败') + '；没有可公开的本次候选，不会自动重试'};
    $('intake-state').textContent = intakeJob ? (intakeJob.bvid + ' · 第 ' + intakeJob.page + ' 分P：' + (Object.hasOwn(states, intakeJob.state) ? states[intakeJob.state] : '当前处理状态待确认')) : intakeRequestId ? '提交结果待确认，正在用原请求编号查询；不会自动重复读取。' : '暂无本次运行的内容提取作业。';
    controls();
  }
  async function refreshIntake() {
    if (!sessionConnected || intakeBusy) return;
    const guard = sessionGeneration, sequence = ++intakeSequence;
    try {
      const current = await api('/app/intake', {signal: AbortSignal.timeout(4000)});
      if (guard !== sessionGeneration || sequence !== intakeSequence) return;
      captureEnabled = current.enabled === true;
      let job = current.job;
      if (selectedIntakeId && !intakeRequestId) job = await api('/app/intake/' + selectedIntakeId, {signal: AbortSignal.timeout(4000)}, true);
      if (intakeRequestId && job?.job_id !== intakeRequestId) job = await api('/app/intake/' + intakeRequestId, {signal: AbortSignal.timeout(4000)}, true);
      if (guard !== sessionGeneration || sequence !== intakeSequence) return;
      intakeJob = job;
      if (job && ['available', 'failed', 'canceled', 'interrupted'].includes(job.state)) intakeRequestId = '';
      renderIntake();
      const history = await api('/app/intake/history', {signal: AbortSignal.timeout(10000)});
      if (guard !== sessionGeneration || sequence !== intakeSequence) return;
      $('intake-history').replaceChildren();
      for (const row of history.jobs) {
        const labels = {available: '已保存', failed: '读取失败，未自动重试', canceled: '已取消', interrupted: '上次运行结果未确认，未自动重试', running: '正在提取', queued: '等待提取', cancel_requested: '正在取消'};
        const item = node('li', `${row.bvid} / p=${row.page} · ${labels[row.state] || '状态待确认'}`);
        if (row.state === 'available') {
          const button = node('button', '查看母版'); button.type = 'button';
          button.disabled = intakeBusy || !!intakeRequestId || ['queued', 'running', 'cancel_requested'].includes(intakeJob?.state);
          button.addEventListener('click', () => {
            if (intakeBusy || intakeRequestId || ['queued', 'running', 'cancel_requested'].includes(intakeJob?.state)) return;
            selectedIntakeId = row.job_id; sourceJobId = ''; refreshIntake();
          }); item.append(button);
        }
        $('intake-history').append(item);
      }
      $('intake-history-hint').textContent = history.jobs.length ? '已保存的母版可以再次使用；打开不会重新读取视频。' : '暂无已记录的提取。';
      if (job?.state === 'available' && job.result_available === true && uuid.test(job.job_id) && sourceJobId !== job.job_id) {
        const sourceGuard = ++sourceSequence;
        const value = await api('/app/intake/' + job.job_id + '/candidate', {signal: AbortSignal.timeout(10000)});
        if (guard !== sessionGeneration || sequence !== intakeSequence || sourceGuard !== sourceSequence) return;
        if (value.state !== 'unverified_source_candidate' || value.formal_task !== false || value.completed !== false || value.source_ref?.kind !== 'audio_transcription' || !Array.isArray(value.segments)) throw new Error();
        sourceCandidate = value; sourceJobId = job.job_id; renderSource(true);
      }
    } catch (error) {
      if (guard !== sessionGeneration || sequence !== intakeSequence) return;
      if (error.status === 401) { sessionConnected = false; connected(); }
      $('intake-state').textContent = error.status === 401 ? '连接已失效，请重新连接。' : '内容提取状态暂时无法确认，请查询状态；不会自动重新提交或读取。';
      controls();
    }
  }
  async function submitIntake() {
    if (intakeBusy || intakeRequestId || !sessionConnected || !captureEnabled || !$('intake-form').reportValidity()) return;
    if (!$('intake-permission').checked) { $('intake-state').textContent = '请勾选本次匿名音轨下载与本机识别许可。'; return; }
    const guard = sessionGeneration; selectedIntakeId = ''; intakeRequestId = crypto.randomUUID(); intakeBusy = true; intakeSequence++; sourceSequence++; sourceJobId = ''; clearSource(); controls();
    try {
      const job = await api('/app/intake', {method: 'POST', signal: AbortSignal.timeout(10000), body: JSON.stringify({request_id: intakeRequestId, source: $('intake-source').value.trim(), allow_local_audio_read: true})});
      if (guard !== sessionGeneration) return;
      intakeJob = job; renderIntake();
    } catch (error) {
      if (guard !== sessionGeneration) return;
      if ([403, 409, 422].includes(error.status)) intakeRequestId = '';
      if (error.status === 401) { sessionConnected = false; connected(); }
      $('intake-state').textContent = error.status === 403 ? '本机内容提取未开放或许可未获准。' : error.status === 409 ? '已有作业或请求冲突，请先查询当前状态。' : error.status === 422 ? '视频链接无效，请核对B站链接和分P。' : '提交结果待确认，保留原请求编号查询；不会自动重复读取。';
    } finally { intakeBusy = false; controls(); }
  }
  async function cancelIntake() {
    if (intakeBusy || !intakeJob || !uuid.test(intakeJob.job_id) || !sessionConnected) return;
    const id = intakeJob.job_id, guard = sessionGeneration; intakeBusy = true; intakeSequence++; controls();
    try { const job = await api('/app/intake/' + id + '/cancel', {method: 'POST', signal: AbortSignal.timeout(10000)}); if (guard === sessionGeneration) { intakeJob = job; renderIntake(); } }
    catch (error) { if (guard === sessionGeneration) { if (error.status === 401) { sessionConnected = false; connected(); } $('intake-state').textContent = '取消结果待确认，请查询状态；已发生的音轨读取无法撤销。'; } }
    finally { intakeBusy = false; controls(); }
  }
  function downloadSource(format) {
    if (!sessionConnected || !sourceCandidate || typeof sourceCandidate[format === 'md' ? 'markdown' : 'srt'] !== 'string') return;
    const value = sourceCandidate;
    const url = URL.createObjectURL(new Blob([value[format === 'md' ? 'markdown' : 'srt']], {type: format === 'md' ? 'text/markdown;charset=utf-8' : 'application/x-subrip;charset=utf-8'}));
    const a = node('a', ''); a.href = url; a.download = '来源候选-' + value.source_ref.bvid + '-p' + value.source_ref.page + '.' + format; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function renderArtifact() {
    $('candidate-panel').hidden = !artifact; if (!artifact) return;
    $('candidate-title').textContent = task?.completed ? '已验收的方法清单' : '方法清单候选';
    $('candidate-note').textContent = task?.completed ? '任务接口已确认完成。' : '这份候选已可使用和下载。独立审查意见不是完成凭证，请自行检查后再验收。';
    $('methods').replaceChildren(); $('unverified').replaceChildren(); $('reviews').replaceChildren();
    const draft = artifact.draft || {};
    const audio = draft.source_ref?.kind === 'audio_transcription' || !!draft.source_ref?.provenance;
    $('candidate-source-note').hidden = !audio;
    $('candidate-source-note').textContent = audio ? '来源为本机语音识别转写，并非平台字幕，识别文字可能有误。时间链接指向语音活动检测（VAD）片段范围，不是逐词时间。独立审查只评价转写支持度，不能认证原音频。' : '';
    for (const [index, method] of (draft.methods || []).entries()) {
      const article = node('article', ''); article.append(node('h3', (index + 1) + '. ' + method.title), node('p', '适用前提：' + (method.applicability || '待确认')));
      for (const [title, items, tag] of [['步骤', method.steps, 'ol'], ['检查点', method.checkpoints, 'ul']]) {
        article.append(node('h4', title)); const list = node(tag, '');
        for (const item of items || []) { const li = node('li', item.text); const link = evidence(item, draft.source_ref); if (link) li.append(link); list.append(li); } article.append(list);
      }
      $('methods').append(article);
    }
    if (draft.unverified?.length) { $('unverified').append(node('h3', '未核实事项')); for (const note of draft.unverified) $('unverified').append(node('p', note)); }
    if (!artifact.reviews?.length) $('reviews').append(node('p', '暂无独立审查意见，候选无需等待审查即可查看。'));
    for (const review of artifact.reviews || []) { const details = node('details', ''); details.append(node('summary', '查看一份审查意见'), node('pre', JSON.stringify(review.review, null, 2))); $('reviews').append(details); }
    controls();
  }
  async function refresh() {
    if (!sessionConnected || !taskId) return;
    const id = taskId, guard = generation, sequence = ++readSequence;
    try {
      const value = await api('/tasks/' + id);
      const candidate = await api('/tasks/' + id + '/artifact', {}, true);
      let processing = null;
      try { processing = await api('/app/tasks/' + id + '/processing', {signal: AbortSignal.timeout(4000)}); } catch (_) { /* Optional process observations never block task or candidate reads. */ }
      if (guard !== generation || sequence !== readSequence) return;
      if (candidate && candidate.revision != null && candidate.revision > value.revision) return refresh();
      const observed = processing?.observation;
      observation = observed && observed.revision === value.revision && observed.contract_version === value.current_version && typeof observed.message === 'string' ? observed : null;
      task = value; artifact = candidate; renderTask(); renderArtifact();
    } catch (error) { if (guard === generation && sequence === readSequence) { say(error.message); if (error.status === 401) { token = ''; sessionConnected = false; connected(); } } }
  }
  async function mutation(action) {
    if (busy) return; busy = true; controls(); const guard = generation;
    try { say(''); await action(); } catch (error) { if (guard === generation) { say(error.message); if (error.status === 409) await refresh(); } }
    finally { busy = false; controls(); }
  }
  async function connectSession() {
    generation++; readSequence++; sessionConnected = false; connected(); const guard = sessionGeneration;
    try { const value = await api('/app/session', token ? {method: 'POST'} : {}); if (guard !== sessionGeneration) return; sessionConnected = value.connected === true; token = ''; connected(); if (sessionConnected) { loadSource(guard); refreshIntake(); await refresh(); } }
    catch (error) { if (guard !== sessionGeneration) return; token = ''; sessionConnected = false; connected(); say(error.message); }
  }
  $('source-download-md').addEventListener('click', () => downloadSource('md'));
  $('source-download-srt').addEventListener('click', () => downloadSource('srt'));
  $('source-use').addEventListener('click', () => {
    if (sessionConnected && sourceCandidate && uuid.test(sourceJobId)) window.dispatchEvent(new CustomEvent('vaa-use-source', {detail: sourceJobId}));
  });
  $('intake-form').addEventListener('submit', event => { event.preventDefault(); submitIntake(); });
  $('intake-refresh').addEventListener('click', () => refreshIntake());
  $('intake-cancel').addEventListener('click', () => cancelIntake());
  $('intake-source').addEventListener('input', () => { $('intake-permission').checked = false; });
  $('connect-form').addEventListener('submit', event => { event.preventDefault(); token = $('token').value.trim(); $('token').value = ''; say(''); connectSession(); });
  $('source-mode').addEventListener('change', () => { $('permission').checked = false; renderMode(); });
  $('request-form').addEventListener('submit', event => { event.preventDefault(); mutation(async () => { const body = input(); const guard = generation; const value = await api('/tasks', {method: 'POST', headers: {'Idempotency-Key': crypto.randomUUID()}, body: JSON.stringify(body)}); if (guard !== generation) return; select(value.task_id); say('任务已提交。'); await refresh(); }); });
  $('open-form').addEventListener('submit', event => { event.preventDefault(); const id = $('task-id').value.trim(); if (!uuid.test(id)) return say('请粘贴完整的任务编号。'); select(id); say(''); refresh(); });
  $('refresh').addEventListener('click', () => { say(''); refresh(); });
  $('revise').addEventListener('click', () => mutation(async () => { const body = input(); const id = taskId, guard = generation; await api('/tasks/' + id + '/revisions', {method: 'POST', body: JSON.stringify({expected_revision: task.revision, request: body})}); if (guard !== generation) return; artifact = null; $('candidate-panel').hidden = true; say('需求已修改，正在重新查询当前任务。'); await refresh(); }));
  $('cancel').addEventListener('click', () => mutation(async () => { const guard = generation; await api('/tasks/' + taskId + '/cancel', {method: 'POST', body: JSON.stringify({expected_revision: task.revision})}); if (guard === generation) { say('取消请求已提交。'); await refresh(); } }));
  $('accept').addEventListener('click', () => mutation(async () => { const guard = generation; await api('/tasks/' + taskId + '/artifact/accept', {method: 'POST', body: JSON.stringify({expected_revision: task.revision, artifact_id: artifact.artifact_id})}); if (guard === generation) { say('验收已提交，正在确认任务状态。'); await refresh(); } }));
  $('download').addEventListener('click', () => { if (!artifact) return; const url = URL.createObjectURL(new Blob([artifact.markdown], {type: 'text/markdown;charset=utf-8'})); const a = node('a', ''); a.href = url; a.download = '方法清单-' + taskId + '.md'; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
  connected();
  try { const saved = localStorage.getItem('video-action-task-id'); if (saved && uuid.test(saved)) select(saved); } catch (_) { /* No sensitive data is stored. */ }
  function refreshService() { fetch('/app/status', {cache: 'no-store'}).then(response => { if (!response.ok) throw new Error(); return response.json(); }).then(status => { capabilities = Array.isArray(status.capabilities) ? status.capabilities : []; executionEnabled = status.execution_enabled === true; captureEnabled = status.source_capture_enabled === true; $('service').textContent = captureEnabled && status.notes_enabled ? '本机内容提取与资料加工已开启。' : executionEnabled ? '本机处理已开启；提交任务后按当前状态查看进展。' : '本机处理部分未开启，可查看已保存内容。'; renderMode(); }).catch(() => { capabilities = []; executionEnabled = false; captureEnabled = false; $('service').textContent = '本机处理状态待确认，请检查助手是否运行。'; renderMode(); }); }
  renderMode();
  connectSession(); refreshService(); setInterval(() => {refresh(); refreshService(); refreshIntake();}, 5000);
})();

