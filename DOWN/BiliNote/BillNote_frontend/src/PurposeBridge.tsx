import React, {useCallback, useEffect, useRef, useState} from 'react'
import {createRoot} from 'react-dom/client'
import {Toaster} from 'react-hot-toast'
import MarkdownViewer from './pages/HomePage/components/MarkdownViewer'
import {useTaskStore, type Task} from './store/taskStore'
import './index.css'
import './PurposeBridge.css'
import SourceReference from './SourceReference'
import SavedSourcePicker from './SavedSourcePicker'
import type {SavedVideoSource} from './savedSources'
import PurposeConversation from './PurposeConversation'
import type {ConversationAnswer, ConversationAdmission} from './purposeConversationSession'
import type {ChatMode} from './pages/HomePage/components/ChatPanelView'
import type {BoundExplanation} from './ExplanationView'
import {chooseSavedContent, parseContentTarget, replaceContentTarget} from './savedContentLink'

type OutputKind = 'structured_notes' | 'source_answer' | 'method_list'
type Source = SavedVideoSource
type Flow = {flow_id: string; source_job_id?: string; notes_job_id?: string; bvid?: string; page?: number; user_request: string; output_kind?: OutputKind; goal_led?: boolean; state: string; source_state?: string; notes_state?: string; result_available?: boolean; error_code?: string | null}
type Job = {generation_model?: string; goal_led?: boolean; explanations?: BoundExplanation[]; solution_status?: 'ready' | 'partial' | 'needs_clarification' | 'unsupported'; solution_gaps?: {kind: string; required: boolean; description: string}[]; revision_note?: string; job_id: string; request_id?: string; source_id?: string; master_id?: string; output_kind: OutputKind; user_request: string; state: string; source_url: string; created_at?: string; result?: {markdown: string; transcript: Task['transcript']; audio_meta: Task['audioMeta']}; bvid?: string; page?: number; formal_task: false; completed: false; vision_state?: string | null; vision_note?: string | null; error_code?: string | null; result_available?: boolean; read_unverified?: boolean; local_edits_available?: boolean; revision?: {root_job_id: string; version: number; parent_job_id?: string | null; record_fingerprint: string}}
type BridgeState = {scope_id?: string; flows: Flow[]; jobs: Job[]; sources: Source[]}
class BridgeApiError extends Error {code?: string; status?: number; admission?: string}
const SELECTED_KEY = 'vaa-purpose-selected-v1'
const PENDING_KEY = 'vaa-purpose-pending-v1'
const REVISION_SELECT_KEY = 'vaa-purpose-revision-select-v1'
const kinds: {value: OutputKind; label: string; prompt: string}[] = [
  {value: 'structured_notes', label: '笔记', prompt: '写下希望整理的内容、结构或边界'},
  {value: 'source_answer', label: '回答问题', prompt: '写下关于这个视频的问题'},
  {value: 'method_list', label: '操作步骤', prompt: '写下希望提取或整理的操作步骤'},
]
const activeStates = new Set(['queued', 'extracting', 'building', 'running'])
function errorMessage(cause: unknown, fallback: string) {
  const messages: Record<string, string> = {
    local_session_required: '本机连接尚未建立或已失效。请从启动入口重新打开视频助手；已有内容会保留。',
    model_outcome_unknown: '模型处理结果还未确认。请先查看处理记录，不要重复提交。',
    model_failed_or_unknown: '本次模型处理结果还未确认。请保留处理记录，不要重复提交。',
    native_failed_or_unconfirmed: '上一次处理未正常结束，结果还未确认。请保留处理记录，不要重复提交。',
    transcription_failed: '声音识别未完成，这次没有生成正文。请保留处理记录；已读取的视频和已有内容仍可使用。',
    previous_run_unconfirmed: '上一次处理结果还未确认。请保留记录，不要重复提交。',
    output_source_changed: '这份成果的来源或内容校验未通过，暂时不能读取。请保留记录，不要继续修改这份成果。',
    output_not_ready: '这份内容还不能读取。请先查看处理进度。',
    output_not_found: '没有找到这份已保存内容，请重新选择。',
    notes_unavailable: '目前没有可读取的正文，请查看处理记录。',
    outputs_busy: '正在处理另一项任务。请先等待，或取消当前任务后再提交。',
    model_text_permission_required: '请先确认本次允许将文字材料发送给模型服务。',
    source_literal_unverified: '部分技术名称无法从已有材料确认，这次结果未保存。来源和旧稿仍可使用。',
    source_review_invalid: '来源复核未能得到可用的修正，这次结果未保存。来源和旧稿仍可使用。',
    source_review_unavailable: '暂时无法进行来源复核，这次结果未保存。来源和旧稿仍可使用。',
    source_inference_unverified: '结果仍含有无法确认的推测，这次未保存。来源和旧稿仍可使用。',
    source_insufficient_for_exact_request: '现有材料不足以提供你要求的全部准确名称或参数，这次未保存为操作稿。可以改为整理已确认的信息，或补充材料后继续。',
    revision_parent_changed: '原稿已发生变化，请重新选择并核对后再修改。',
    revision_parent_superseded: '已有更新的版本，请先选择新版本再修改。',
    output_request_conflict: '这次请求与已记录的内容不一致。请先查看处理记录，不要重复提交。',
    invalid_followup_request: '这次追问没有对应的已保存内容，请重新打开原内容后继续。',
    followup_parent_changed: '所接续的内容已变化或暂时不可用，请重新打开核对。',
    followup_context_limit: '这段对话已达到本机长度上限。已有内容会保留，可以就同一视频独立提问。',
  }
  if (cause instanceof Error) {
    const code = (cause as Error & {code?: string}).code || cause.message
    if (messages[code]) return messages[code]
    if (cause instanceof TypeError && /fetch|network/i.test(cause.message)) {
      return '暂时无法连接本机服务。请从启动入口打开视频助手，再检查连接。'
    }
    if (/[\u3400-\u9fff]/.test(cause.message)) return cause.message
  }
  return fallback
}
async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/bridge/${path}`, {...options, credentials: 'same-origin', headers: {'Content-Type': 'application/json', ...options.headers}, cache: 'no-store'})
  const value = await response.json().catch(() => ({})) as Record<string, unknown>
  if (!response.ok) {
    const message = [value.detail, value.error].find(item => typeof item === 'string')
    const error = new BridgeApiError(typeof message === 'string' ? message : `请求失败（HTTP ${response.status}）`)
    const code = value.error_code || value.code
    if (typeof code === 'string') error.code = code
    error.status = response.status
    if (typeof value.admission === 'string') error.admission = value.admission
    throw error
  }
  return value as T
}
function contentLabel(job: Job) { return job.goal_led ? '内容' : kindLabel(job.output_kind) }
function kindLabel(kind?: OutputKind) { return kinds.find(item => item.value === kind)?.label || '用途待确认' }
function savedJobTitle(job: Job, sources: Source[]) {
  return sources.find(source => source.source_id === job.source_id)?.title?.trim() || job.user_request || job.bvid || '已保存内容'
}
function savedJobTime(job: Job) {
  const time = Date.parse(job.created_at || '')
  return Number.isFinite(time) ? time : 0
}
function savedJobDate(job: Job) {
  const time = savedJobTime(job)
  return time ? new Date(time).toLocaleString('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}) : ''
}
function exactEditError(markdown: string, before: string, after: string) {
  if (!before.trim()) return '请填写要替换的原文。'
  if (before.length > 4000) return '原文不能超过 4000 个字符，请缩小选段。'
  if (after.length > 8000) return '替换内容不能超过 8000 个字符。'
  if (/\r?\n[ \t]*\r?\n/.test(before)) return '原文不能跨空白段落，请只选同一段中的文字。'
  const paragraphs = markdown.split(/\r?\n[ \t]*\r?\n+/)
  let matches = 0
  for (const paragraph of paragraphs) {
    let at = paragraph.indexOf(before)
    while (at >= 0) {
      matches += 1
      at = paragraph.indexOf(before, at + 1)
    }
  }
  if (matches > 1) return '原文在正文中重复出现，请加入上下文缩小到唯一位置。'
  if (matches === 0) return markdown.includes(before) ? '原文跨越空白段落，请只选同一段中的文字。' : '原文与当前成果不匹配，请从所选正文重新复制。'
  return ''
}
function stateLabel(state: string, kind?: OutputKind, goalLed = false) {
  const label = goalLed ? '内容' : kindLabel(kind)
  if (state === 'queued') return '排队中'
  if (state === 'extracting') return '正在读取视频内容'
  if (state === 'building' || state === 'running') return `正在生成${label}`
  if (state === 'available') return `${label}已保存，待读取核对`
  if (state === 'failed') return '处理失败'
  if (state === 'interrupted') return '处理已中断，结果待确认'
  if (state === 'canceled') return '已取消'
  return '状态待确认'
}
function PurposeBridge() {
  const linkTargetRef = useRef<string | null>(parseContentTarget(location.search))
  const linkTargetRetryPendingRef = useRef(false)
  const linkTargetRetryUsedRef = useRef(false)
  const [connected, setConnected] = useState(false)
  const [stateLoaded, setStateLoaded] = useState(false)
  const [flows, setFlows] = useState<Flow[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [savedQuery, setSavedQuery] = useState('')
  const [selectedCandidate, setSelectedCandidate] = useState<Job | null>(null)
  const [candidateLoading, setCandidateLoading] = useState(false)
  const [candidateError, setCandidateError] = useState('')
  const [sources, setSources] = useState<Source[]>([])
  const [inputMode, setInputMode] = useState<'link' | 'saved'>('link')
  const [source, setSource] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [conversationOpen, setConversationOpen] = useState(false)
  const [conversationMode, setConversationMode] = useState<ChatMode>('half')
  const [kind] = useState<OutputKind>('structured_notes')
  const [request, setRequest] = useState('')
  const [allowLocalAudio, setAllowLocalAudio] = useState(false)
  const [allowLocalVideo, setAllowLocalVideo] = useState(false)
  const [allowModelText, setAllowModelText] = useState(false)
  const [allowModelImages, setAllowModelImages] = useState(false)
  const [useSavedVisuals, setUseSavedVisuals] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [selected, setSelected] = useState(() => linkTargetRef.current !== null ? linkTargetRef.current : sessionStorage.getItem(SELECTED_KEY) || '')
  const [flowId, setFlowId] = useState('')
  const [pendingId, setPendingId] = useState(() => sessionStorage.getItem(PENDING_KEY) || '')
  const [lastRequestId, setLastRequestId] = useState(() => sessionStorage.getItem(PENDING_KEY) || '')
  const [revisionInstruction, setRevisionInstruction] = useState('')
  const [allowRevision, setAllowRevision] = useState(false)
  const [revisionMode, setRevisionMode] = useState<'local' | 'model'>('local')
  const [editBefore, setEditBefore] = useState('')
  const [editAfter, setEditAfter] = useState('')
  const [error, setError] = useState(() => linkTargetRef.current === '' ? '链接中的成果编号无效或重复；请手动选择已保存内容。' : '')
  const [canceling, setCanceling] = useState('')
  const polling = useRef(false)
  const selectedRef = useRef(selected)
  const candidateRef = useRef<Job | null>(null)
  const candidateRequestRef = useRef(0)
  const candidateLoadIdRef = useRef('')
  const revisionParentRef = useRef('')
  const pendingRef = useRef(pendingId)
  const submissionLock = useRef('')
  const revisionSelectRef = useRef(linkTargetRef.current !== null ? '' : sessionStorage.getItem(REVISION_SELECT_KEY) || '')
  const clearGrants = () => { setAllowLocalAudio(false); setAllowLocalVideo(false); setAllowModelText(false); setAllowModelImages(false); setUseSavedVisuals(false) }

  const readCandidate = useCallback(async (jobId: string, retry = false) => {
    if (!jobId || (!retry && candidateLoadIdRef.current === jobId)) return
    const requestNo = ++candidateRequestRef.current
    candidateLoadIdRef.current = jobId
    candidateRef.current = null
    setSelectedCandidate(null)
    setCandidateError('')
    setCandidateLoading(true)
    useTaskStore.setState({tasks: [], currentTaskId: ''})
    try {
      const job = await api<Job>(`outputs/${encodeURIComponent(jobId)}`)
      if (requestNo !== candidateRequestRef.current || selectedRef.current !== jobId) return
      if (job.job_id !== jobId || !job.result || typeof job.result.markdown !== 'string') {
        throw new Error('服务未返回经过核验的成果正文。')
      }
      const revisionParent = `${job.job_id}:${job.revision?.record_fingerprint || ''}`
      if (revisionParentRef.current !== revisionParent) {
        revisionParentRef.current = revisionParent
        setRevisionInstruction('')
        setAllowRevision(false)
        setEditBefore('')
        setEditAfter('')
        setRevisionMode(job.local_edits_available ? 'local' : 'model')
        setError('')
      }
      candidateRef.current = job
      setSelectedCandidate(job)
      const task: Task & {formData: Task['formData'] & {style: string}} = {
        id: job.job_id, status: 'SUCCESS',
        markdown: job.result.markdown.split('(/bridge/assets/').join('(/api/bridge/assets/'),
        transcript: job.result.transcript, audioMeta: job.result.audio_meta || {}, createdAt: job.created_at || '',
        formData: {model_name: 'generation_engine' in job && job.generation_engine === 'local_exact_edits_with_bilinote_save' ? '本地修改' : job.generation_model || '模型未记录', style: '', provider_id: 'configured-provider', video_url: job.source_url, platform: 'bilibili', quality: '', screenshot: false, link: false},
      }
      useTaskStore.setState({tasks: [task], currentTaskId: jobId})
    } catch (cause: unknown) {
      if (requestNo === candidateRequestRef.current && selectedRef.current === jobId) {
        candidateRef.current = null
        setCandidateError(`当前结果不可读取：${errorMessage(cause, '读取核验失败')}`)
      }
    } finally {
      if (requestNo === candidateRequestRef.current) setCandidateLoading(false)
    }
  }, [])

  const refresh = useCallback(async () => {
    if (polling.current) return
    polling.current = true
    try {
      const value = await api<BridgeState>('state')
      const nextFlows = Array.isArray(value.flows) ? value.flows : []
      const nextJobs = Array.isArray(value.jobs) ? value.jobs : []
      const nextSources = Array.isArray(value.sources) ? value.sources : []
      setFlows(nextFlows); setJobs(nextJobs); setSources(nextSources); setStateLoaded(true)
      if (pendingRef.current && (nextFlows.some(flow => flow.flow_id === pendingRef.current) || nextJobs.some(job => job.job_id === pendingRef.current))) {
        if (submissionLock.current === pendingRef.current) {
          submissionLock.current = ''; setSubmitting(false)
        }
        sessionStorage.removeItem(PENDING_KEY); pendingRef.current = ''; setPendingId(''); setError('请求状态已在记录中找到。')
      }
      const selectedAtResponse = selectedRef.current
      const available = nextJobs.filter(job => job.state === 'available')
      const requestedFlow = nextFlows.find(flow => flow.flow_id === revisionSelectRef.current)
      const revisionJob = available.find(job => job.job_id === revisionSelectRef.current || job.job_id === requestedFlow?.notes_job_id)
      const chosen = chooseSavedContent(linkTargetRef.current, revisionJob?.job_id || '', selectedAtResponse, available.map(job => job.job_id))
      if (revisionJob) {
        revisionSelectRef.current = ''
        sessionStorage.removeItem(REVISION_SELECT_KEY)
      }
      if (chosen !== selectedAtResponse) {
        setConversationOpen(false)
        selectedRef.current = chosen
        candidateRequestRef.current += 1
        candidateLoadIdRef.current = ''
        candidateRef.current = null
        setSelectedCandidate(null)
        setCandidateError('')
        setCandidateLoading(false)
        revisionParentRef.current = ''
        setError('')
        setSelected(chosen)
        setRevisionInstruction('')
        setAllowRevision(false)
        setRevisionMode('local')
        setEditBefore('')
        setEditAfter('')
        sessionStorage.setItem(SELECTED_KEY, chosen)
        useTaskStore.setState({tasks: [], currentTaskId: ''})
      }
      if (linkTargetRef.current && available.some(job => job.job_id === linkTargetRef.current) && linkTargetRetryPendingRef.current && !linkTargetRetryUsedRef.current && candidateRef.current?.job_id !== linkTargetRef.current) {
        linkTargetRetryPendingRef.current = false
        linkTargetRetryUsedRef.current = true
        void readCandidate(linkTargetRef.current, true)
      }
      if (revisionJob || (chosen && !candidateRef.current && candidateLoadIdRef.current !== chosen)) {
        if (linkTargetRef.current === chosen && !available.some(job => job.job_id === chosen)) linkTargetRetryPendingRef.current = true
        void readCandidate(chosen)
      }
      if (revisionJob) replaceContentTarget(revisionJob.job_id)
    } finally { polling.current = false }
  }, [readCandidate])

  useEffect(() => {
    let alive = true
    const token = new URLSearchParams(location.hash.slice(1)).get('token')
    if (token) history.replaceState(null, '', `${location.pathname}${location.search}`)
    const start = token ? api('session', {method: 'POST', body: JSON.stringify({token})}) : api('state')
    start.then(() => { if (alive) setConnected(true) }).catch((cause: unknown) => { if (alive) setError(errorMessage(cause, '连接本机服务失败')) })
    return () => { alive = false }
  }, [])
  useEffect(() => {
    if (!connected) return
    let timer: number | undefined; let alive = true
    const tick = async () => { try { await refresh() } catch (cause: unknown) { if (alive) setError(errorMessage(cause, '读取状态失败')) }; if (alive) timer = window.setTimeout(tick, 2000) }
    void tick(); return () => { alive = false; if (timer) window.clearTimeout(timer) }
  }, [connected, refresh])

  const readableJobs = jobs.filter(job => job.state === 'available')
  const selectedJob = selectedCandidate?.job_id === selected && selectedCandidate.result?.markdown ? selectedCandidate : null
  const explanations = Array.isArray(selectedJob?.explanations) ? selectedJob.explanations : []
  const reader = selectedJob
    ? <div className="vaa-purpose-reader"><MarkdownViewer key={selectedJob.job_id} status="success" content="" allowMindmap={false} allowTranscript={false} inlineExplanations={explanations}/></div>
    : null

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!request.trim() || submitting || submissionLock.current || pendingRef.current) return
    if (inputMode === 'link' && (!source.trim() || !allowLocalAudio || !allowModelText)) return
    if (inputMode === 'saved' && (!sourceId || !allowModelText)) return
    linkTargetRef.current = null
    linkTargetRetryPendingRef.current = false
    linkTargetRetryUsedRef.current = false
    replaceContentTarget()
    const requestId = crypto.randomUUID()
    setLastRequestId(requestId); setFlowId(''); pendingRef.current = requestId; setPendingId(requestId); sessionStorage.setItem(PENDING_KEY, requestId)
    revisionSelectRef.current = requestId; sessionStorage.setItem(REVISION_SELECT_KEY, requestId)
    setSubmitting(true); setError(''); clearGrants()
    try {
      if (inputMode === 'link') {
        const response = await api<{flow_id: string}>('flows', {method: 'POST', body: JSON.stringify({request_id: requestId, source, user_request: request, output_kind: kind, allow_local_audio_read: true, allow_local_video_read: allowLocalVideo, allow_model_text: true, allow_model_images: allowModelImages})})
        if (!response.flow_id) throw new Error('服务未返回流程编号，请查询请求记录确认状态。')
        setFlowId(response.flow_id)
        if (revisionSelectRef.current === requestId) { revisionSelectRef.current = response.flow_id; sessionStorage.setItem(REVISION_SELECT_KEY, response.flow_id) }
      } else {
        const response = await api<Job>('outputs', {method: 'POST', body: JSON.stringify({request_id: requestId, source_id: sourceId, user_request: request, output_kind: kind, allow_model_text: true, use_saved_visuals: useSavedVisuals, allow_model_images: useSavedVisuals && allowModelImages})})
        if (response.job_id && revisionSelectRef.current === requestId) { revisionSelectRef.current = response.job_id; sessionStorage.setItem(REVISION_SELECT_KEY, response.job_id) }
      }
      try { await refresh() } catch { setError('请求已确认接收，但暂时无法更新状态；页面会继续只读查询。') }
    } catch (cause: unknown) {
      if (cause instanceof BridgeApiError && (cause.admission === 'rejected_before_create' || cause.status === 422)) {
        sessionStorage.removeItem(PENDING_KEY); pendingRef.current = ''; setPendingId('')
        if (revisionSelectRef.current === requestId) { revisionSelectRef.current = ''; sessionStorage.removeItem(REVISION_SELECT_KEY) }
        setError(`${errorMessage(cause, '请求未创建')}；可修改后手动提交。`)
      } else { setError(`${errorMessage(cause, '提交结果未确认')}；不会自动重发，请查看请求记录并等待状态查询。`) }
    } finally { setSubmitting(false) }
  }
  async function reviseSelectedJob(event: React.FormEvent) {
    event.preventDefault()
    if (!selectedJob?.revision?.record_fingerprint || submitting || submissionLock.current || pendingRef.current || activeStates.has(selectedJob.state)) return
    const currentParent = candidateRef.current
    if (selectedRef.current !== selectedJob.job_id || currentParent?.job_id !== selectedJob.job_id || currentParent.revision?.record_fingerprint !== selectedJob.revision.record_fingerprint) {
      setError('所选成果已变化，请重新选择并核对后再修改。')
      return
    }
    const useLocalEdit = revisionMode === 'local' && selectedJob.local_edits_available === true
    if (!useLocalEdit && (!revisionInstruction.trim() || !allowRevision)) return
    if (useLocalEdit) {
      const problem = exactEditError(selectedJob.result?.markdown || '', editBefore, editAfter)
      if (problem) { setError(problem); return }
    }
    linkTargetRef.current = null
    linkTargetRetryPendingRef.current = false
    linkTargetRetryUsedRef.current = false
    replaceContentTarget()
    const requestId = crypto.randomUUID()
    setLastRequestId(requestId)
    pendingRef.current = requestId
    setPendingId(requestId)
    sessionStorage.setItem(PENDING_KEY, requestId)
    revisionSelectRef.current = requestId
    sessionStorage.setItem(REVISION_SELECT_KEY, requestId)
    setSubmitting(true)
    setError('')
    setAllowRevision(false)
    try {
      const child = await api<Job>('revisions', {method: 'POST', body: JSON.stringify({
        request_id: requestId,
        parent_job_id: selectedJob.job_id,
        expected_parent_fingerprint: selectedJob.revision.record_fingerprint,
        instruction: useLocalEdit ? '按用户输入替换指定文字' : revisionInstruction,
        ...(useLocalEdit
          ? {allow_model_text: false, allow_local_edits: true, exact_edits: [{before: editBefore, after: editAfter}]}
          : {allow_model_text: true}),
      })})
      if (child.job_id) {
        if (revisionSelectRef.current === requestId) {
          revisionSelectRef.current = child.job_id
          sessionStorage.setItem(REVISION_SELECT_KEY, child.job_id)
        }
      }
      try { await refresh() } catch { setError('修改稿请求已确认接收，页面会继续只读查询，完成后自动切换到新版本。') }
    } catch (cause: unknown) {
      if (cause instanceof BridgeApiError && (cause.admission === 'rejected_before_create' || cause.status === 422)) {
        sessionStorage.removeItem(PENDING_KEY); pendingRef.current = ''; setPendingId('')
        sessionStorage.removeItem(REVISION_SELECT_KEY); revisionSelectRef.current = ''
        setError(`${errorMessage(cause, '修改稿未创建')}；可核对要求后再次提交。`)
      } else {
        setError(`${errorMessage(cause, '修改稿提交结果未确认')}；不会自动重发，请等待状态查询。`)
      }
    } finally { setSubmitting(false) }
  }
  async function downloadJob(job: Job) {
    if (selectedRef.current !== job.job_id || candidateRef.current?.job_id !== job.job_id) {
      setError('当前结果尚未读取核验，不能下载。')
      return
    }
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(job.job_id)) { setError('成果编号无效，无法下载。'); return }
    setError('')
    try {
      const response = await fetch(`/api/bridge/notes/${job.job_id}/download`, {credentials: 'same-origin', cache: 'no-store'})
      if (!response.ok) { const value = await response.json().catch(() => ({})) as Record<string, unknown>; throw new Error(typeof value.detail === 'string' ? value.detail : `下载失败（HTTP ${response.status}）`) }
      const disposition = response.headers.get('Content-Disposition') || ''
      const declaredName = disposition.match(/filename\*?=(?:UTF-8''|")?([^;"]+)/i)?.[1]
      const filename = declaredName ? decodeURIComponent(declaredName.trim()) : `${contentLabel(job)}.md`
      const url = URL.createObjectURL(await response.blob()); const link = document.createElement('a'); link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (cause: unknown) { setError(errorMessage(cause, '成果下载失败')) }
  }
  async function cancelFlow(flow: Flow) { setCanceling(flow.flow_id); setError(''); try { await api('cancel', {method: 'POST', body: JSON.stringify({flow_id: flow.flow_id})}); await refresh() } catch (cause: unknown) { setError(`${errorMessage(cause, '取消结果未确认')}；请查看状态记录。`) } finally { setCanceling('') } }
  async function cancelJob(job: Job) { setCanceling(job.job_id); setError(''); try { await api('cancel', {method: 'POST', body: JSON.stringify({job_id: job.job_id})}); await refresh() } catch (cause: unknown) { setError(`${errorMessage(cause, '取消结果未确认')}；请查看状态记录。`) } finally { setCanceling('') } }

  async function submitQuestion(id: string, question: string, parent: ConversationAnswer, basis: ConversationAnswer): Promise<ConversationAdmission> {
    const current = candidateRef.current
    if (selectedRef.current !== basis.job_id || current?.revision?.record_fingerprint !== basis.revision?.record_fingerprint) {
      return {status: 'rejected', message: '正在阅读的内容已变化，请在新内容中提问。'}
    }
    if (submissionLock.current || submitting || pendingRef.current || jobs.some(job => activeStates.has(job.state)) || flows.some(flow => activeStates.has(flow.state))) {
      return {status: 'rejected', message: '正在处理另一项任务，请先等待或取消它。'}
    }
    submissionLock.current = id
    pendingRef.current = id; setPendingId(id); sessionStorage.setItem(PENDING_KEY, id)
    setLastRequestId(id); setSubmitting(true); clearGrants()
    const clearPending = () => {
      if (pendingRef.current !== id) return
      pendingRef.current = ''; setPendingId(''); sessionStorage.removeItem(PENDING_KEY)
    }
    try {
      const accepted = await api<Job>('outputs', {method: 'POST', body: JSON.stringify({
        request_id: id, source_id: parent.source_id, user_request: question, output_kind: 'source_answer',
        allow_model_text: true,
        followup: {parent_job_id: parent.job_id, expected_parent_fingerprint: parent.revision?.record_fingerprint},
      })})
      if (accepted.job_id !== id) throw new Error('服务未确认本次请求编号。')
      linkTargetRef.current = null
      linkTargetRetryPendingRef.current = false
      linkTargetRetryUsedRef.current = false
      replaceContentTarget()
      clearPending()
      void refresh().catch(() => {})
      return {status: 'accepted'}
    } catch (cause) {
      if (cause instanceof BridgeApiError && (cause.admission === 'rejected_before_create' || cause.status === 422)) {
        clearPending()
        return {status: 'rejected', message: errorMessage(cause, '本次请求未创建，请核对后提交。')}
      }
      return {status: 'unknown', message: errorMessage(cause, '提交结果尚未确认，请等待处理记录。')}
    } finally {
      if (submissionLock.current === id) {submissionLock.current = ''; setSubmitting(false)}
    }
  }
  async function cancelQuestion(id: string) {
    await api('cancel', {method: 'POST', body: JSON.stringify({job_id: id})})
    await refresh()
  }

  const flowRows = flows.filter(flow => !flow.notes_job_id || !jobs.some(job => job.job_id === flow.notes_job_id))
  const requestLabel = '任务要求'
  const selectedSource = sources.find(item => item.source_id === sourceId)
  const linkVisualReady = allowLocalVideo && allowModelImages
  const savedVisualReady = inputMode === 'saved' && useSavedVisuals && Boolean(selectedSource?.saved_visual_available)
  const helpLine = inputMode === 'saved'
    ? savedVisualReady
      ? allowModelImages
        ? '本次使用已保存的声音文字，并将已保存画面发送给模型补充分析；不会重新读取视频。'
        : '本次使用已保存的声音文字和画面观察；不重新读取视频，也不发送画面。'
      : '未选画面时按已保存的声音文字生成；不会重新读取或识别视频。'
    : linkVisualReady
      ? allowLocalAudio
        ? '本次已允许读取并发送选取画面，也允许参考声音文字。'
        : '本次已允许读取并发送选取画面；声音材料需单独授权。'
      : allowLocalVideo
        ? '已允许本机读取画面；尚未允许将画面发送给模型，仍可仅依据已授权的文字生成。'
        : allowModelImages
          ? '已允许将选取画面发送给模型；尚未允许本机读取画面，仍可仅依据已授权的文字生成。'
          : '可仅授权声音文字；画面读取和发送许可可分别选择。'
  const canRevise = Boolean(selectedJob && selectedJob.state === 'available' && selectedJob.revision?.record_fingerprint)
  function chooseJob(next: string) {
    if (!jobs.some(job => job.job_id === next && job.state === 'available')) return
    linkTargetRef.current = null
    linkTargetRetryPendingRef.current = false
    linkTargetRetryUsedRef.current = false
    replaceContentTarget(next)
    selectedRef.current = next
    revisionSelectRef.current = ''
    sessionStorage.removeItem(REVISION_SELECT_KEY)
    setSelected(next)
    setConversationOpen(false)
    sessionStorage.setItem(SELECTED_KEY, next)
    setRevisionInstruction('')
    setAllowRevision(false)
    setRevisionMode('local')
    setEditBefore('')
    setEditAfter('')
    revisionParentRef.current = ''
    setError('')
    void readCandidate(next, true)
  }

  const actionLabel = '开始处理'
  const submitHint = submitting ? '正在提交，请稍候。' : pendingId ? '正在确认上一次提交，请先等待处理记录更新。'
    : inputMode === 'link' && !source.trim() ? '先粘贴一个公开视频链接。'
      : inputMode === 'saved' && !selectedSource ? '先选择一个已读取的视频。'
        : !request.trim() ? kind === 'source_answer' ? '写下你想知道的问题。' : '写下整理要求，也可以使用下方的常用要求。'
          : inputMode === 'link' && !allowLocalAudio ? '请确认本次允许在本机读取视频声音。'
            : !allowModelText ? '请确认本次允许将文字材料发送给模型服务。' : ''
  const recentJobs = [...readableJobs].sort((a, b) => savedJobTime(b) - savedJobTime(a))
  const savedTerms = savedQuery.normalize('NFKC').toLowerCase().trim().split(/\s+/).filter(Boolean)
  const matchingJobs = recentJobs.filter(job => {
    const text = [savedJobTitle(job, sources), job.user_request, job.bvid || '', job.page ? `P${job.page}` : '']
      .join('\n').normalize('NFKC').toLowerCase()
    return savedTerms.every(term => text.includes(term))
  })
  const activeFlows = flowRows.filter(flow => activeStates.has(flow.state))
  const activeJobs = jobs.filter(job => activeStates.has(job.state))
  const selectedTitle = selectedJob ? sources.find(item => item.source_id === selectedJob.source_id)?.title || selectedJob.bvid || '已保存内容' : ''
  function openComposer() {
    const composer = document.getElementById('vaa-composer') as HTMLDetailsElement | null
    if (composer) { composer.open = true; composer.scrollIntoView({block: 'start'}) }
    document.getElementById('vaa-request')?.focus()
  }
  function openEditor() {
    const editor = document.getElementById('vaa-editor') as HTMLDetailsElement | null
    if (editor) { editor.open = true; editor.scrollIntoView({block: 'start'}) }
  }
  function askAboutSelected() {
    if (!selectedJob?.source_id || !selectedJob.revision?.record_fingerprint || !sources.some(item => item.source_id === selectedJob.source_id)) return
    setConversationOpen(true); setConversationMode('half')
  }
  async function retryConnection() {
    setError('')
    try { await api('state'); setConnected(true) } catch (cause: unknown) { setError(errorMessage(cause, '暂时未能建立连接，请从启动入口重新打开。')) }
  }
  function flowRecord(flow: Flow) {
    return <article key={flow.flow_id} className="vaa-record">
      <div className="vaa-actions"><strong>{stateLabel(flow.state, flow.output_kind, flow.goal_led)}</strong><span>{flow.bvid}{flow.page ? ` · P${flow.page}` : ''}</span>
        {activeStates.has(flow.state) && <button type="button" className="vaa-secondary" disabled={canceling === flow.flow_id} onClick={() => void cancelFlow(flow)}>{canceling === flow.flow_id ? '正在取消…' : '取消处理'}</button>}</div>
      <p>{flow.user_request}</p>
      {flow.error_code && <p className="vaa-hint">{errorMessage(new Error(flow.error_code), '本次未能完成，请展开记录查看详情。')}</p>}
      <details className="vaa-hint"><summary>技术记录</summary><p>flow_id：{flow.flow_id}</p><p>来源：{flow.source_state || '未提供'} · 结果：{flow.notes_state || '未提供'}</p>{flow.error_code && <p>错误编号：{flow.error_code}</p>}</details>
    </article>
  }
  function jobRecord(job: Job) {
    return <article key={job.job_id} className="vaa-record">
      <div className="vaa-actions"><strong>{stateLabel(job.state, job.output_kind, job.goal_led)}</strong><span>{job.bvid || '已读取视频'}{job.page ? ` · P${job.page}` : ''} · v{job.revision?.version || 1}</span>
        {activeStates.has(job.state) && <button type="button" className="vaa-secondary" disabled={canceling === job.job_id} onClick={() => void cancelJob(job)}>{canceling === job.job_id ? '正在取消…' : '取消处理'}</button>}
        {job.state === 'available' && <button type="button" className="vaa-secondary" onClick={() => {chooseJob(job.job_id); document.getElementById('vaa-result')?.scrollIntoView({block: 'start'})}}>阅读</button>}</div>
      <p>{job.user_request}</p>
      {job.error_code && <p className="vaa-hint">{errorMessage(new Error(job.error_code), '本次未能完成，请展开记录查看详情。')}</p>}
      <details className="vaa-hint"><summary>技术记录</summary><p>job_id：{job.job_id}</p><p>用途：{contentLabel(job)} · 版本：v{job.revision?.version || 1}</p>{job.vision_note && <p>{job.vision_note}</p>}{job.error_code && <p>错误编号：{job.error_code}</p>}</details>
    </article>
  }
  return <div className="vaa-app">
    <Toaster/>
    <style>{`
      .vaa-purpose-reader button:has(svg.lucide-download),.vaa-purpose-reader button:has(svg.lucide-message-square){display:none}
      .vaa-purpose-reader .markdown-body table{display:table;table-layout:fixed;width:100%}
      .vaa-purpose-reader .markdown-body th,.vaa-purpose-reader .markdown-body td{white-space:normal;overflow-wrap:anywhere}
      .vaa-app .vaa-history-summary{display:-webkit-box;overflow:hidden;-webkit-box-orient:vertical;-webkit-line-clamp:2;overflow-wrap:anywhere;margin-top:5px;color:var(--vaa-muted);font-size:12px;line-height:1.4}
    `}</style>
    <header className="vaa-topbar"><div><h1 className="vaa-brand">视频助手</h1><p className="vaa-subtitle">视频内容整理与问答</p></div><span className="vaa-connection">{connected ? '本机已连接' : '等待连接'}</span></header>
    <div className="vaa-layout">
      <aside className="vaa-sidebar" aria-label="我的内容">
        <button type="button" className="vaa-primary" onClick={openComposer} disabled={!connected}>新建任务</button>
        <div className="vaa-sidebar-heading"><h2>已保存内容</h2>{stateLoaded && <span>{savedTerms.length ? `${matchingJobs.length} / ${readableJobs.length}` : readableJobs.length}</span>}</div>
        {connected && stateLoaded && readableJobs.length > 0 && <label className="vaa-saved-search"><span className="sr-only">搜索已保存内容</span><input type="search" value={savedQuery} onChange={event => setSavedQuery(event.target.value)} placeholder="搜索标题、要求或 BV 编号"/></label>}
        {!connected ? <p className="vaa-sidebar-empty">连接后可查看已保存内容。</p> : !stateLoaded ? <p className="vaa-sidebar-empty" role="status">正在读取…</p> : recentJobs.length === 0 ? <p className="vaa-sidebar-empty">还没有保存内容。完成第一次处理后，会保存在这里。</p> : matchingJobs.length === 0 ? <p className="vaa-sidebar-empty">没有匹配的内容。<button type="button" className="vaa-secondary" onClick={() => setSavedQuery('')}>清空搜索</button></p> : <nav className="vaa-history-list" aria-label="已保存内容">
          {matchingJobs.map(job => <button type="button" key={job.job_id} className={`vaa-history-item${selected === job.job_id ? ' is-selected' : ''}`} aria-current={selected === job.job_id ? 'true' : undefined} title={`${savedJobTitle(job, sources)}\n${job.user_request}`} onClick={() => {chooseJob(job.job_id); document.getElementById('vaa-result')?.scrollIntoView({block: 'start'})}}>
            <span className="vaa-history-title">{savedJobTitle(job, sources)}</span><span className="vaa-history-summary">{job.user_request}</span><span className="vaa-history-meta">{contentLabel(job)} · v{job.revision?.version || 1} · {job.bvid || '已读取视频'}{job.page ? ` · P${job.page}` : ''}{savedJobDate(job) && <><br/><time dateTime={job.created_at}>{savedJobDate(job)}</time></>}</span>
          </button>)}
        </nav>}
      </aside>
      <main className="vaa-main">
        {!connected ? <section className="vaa-card vaa-empty" aria-label="连接视频助手"><h2>先打开视频助手</h2><p role="status">{error || '正在检查本机连接…'}</p><p className="vaa-hint">请在项目文件夹双击 Start-VideoAssistant.cmd，等待页面打开。启动不会自动处理视频，已有内容会保留。</p><button type="button" className="vaa-secondary" onClick={() => void retryConnection()}>重新检查连接</button></section> : <>
          {error && <p role="alert" className="vaa-message is-error">{error}</p>}
          <details id="vaa-composer" className="vaa-card vaa-composer" open={!selectedJob}>
            <summary className="vaa-composer-title">新建任务</summary>
            <form onSubmit={submit}>
              <section className="vaa-form-section"><h2><span className="vaa-number">1</span>选择视频</h2>
                <fieldset className="vaa-choice-row"><legend className="sr-only">视频来源</legend>
                  <label><input type="radio" name="source-mode" checked={inputMode === 'link'} onChange={() => {setInputMode('link'); clearGrants()}}/>新视频链接</label>
                  <label><input type="radio" name="source-mode" checked={inputMode === 'saved'} onChange={() => {setInputMode('saved'); clearGrants()}}/>已读取的视频</label>
                </fieldset>
                {inputMode === 'link' ? <label className="vaa-field"><span>公开视频链接</span><input type="url" value={source} onChange={event => {setSource(event.target.value); clearGrants()}} required placeholder="粘贴 B 站视频链接，支持链接中的分P"/></label> : <SavedSourcePicker sources={sources} value={sourceId} onChange={nextSourceId => {setSourceId(nextSourceId); clearGrants()}}/>}
              </section>
              <section className="vaa-form-section"><h2><span className="vaa-number">2</span>设置任务</h2>
                
                <label className="vaa-field"><span>{requestLabel}</span><textarea id="vaa-request" rows={3} value={request} onChange={event => {setRequest(event.target.value); clearGrants()}} required placeholder="例如：整理课程笔记，或解释两个概念的区别"/></label>
                {kind === 'structured_notes' && <div><button id="vaa-note-example" type="button" className="vaa-secondary" aria-describedby="vaa-note-example-hint" onClick={() => {setRequest('将视频的主要内容整理为便于阅读和查阅的笔记，保留关键概念和必要细节；未说明的信息不要补写。'); clearGrants()}}>{request.trim() ? '替换为笔记要求示例' : '填入笔记要求示例'}</button><p id="vaa-note-example-hint" className="vaa-hint">示例可以修改；点击“开始处理”才会执行。</p></div>}
              </section>
              <section className="vaa-form-section"><h2><span className="vaa-number">3</span>确认本次处理范围</h2>
                <div className="vaa-consent">
                  {inputMode === 'link' && <label><input type="checkbox" checked={allowLocalAudio} onChange={event => setAllowLocalAudio(event.target.checked)}/><span>允许在本机读取这个视频的声音内容</span></label>}
                  <label><input type="checkbox" checked={allowModelText} onChange={event => setAllowModelText(event.target.checked)}/><span>允许将{inputMode === 'saved' ? '已保存的文字' : '必要的文字'}发送给已配置的模型服务，用于完成本次要求</span></label>
                </div>
                {inputMode === 'link' ? <details className="vaa-optional"><summary>补充画面（可选）</summary><div className="vaa-consent">
                  <label><input type="checkbox" checked={allowLocalVideo} onChange={event => setAllowLocalVideo(event.target.checked)}/><span>允许在本机读取这个视频的画面</span></label>
                  <label><input type="checkbox" checked={allowModelImages} onChange={event => setAllowModelImages(event.target.checked)}/><span>允许将选取的画面发送给模型服务</span></label>
                  <p className="vaa-hint">画面读取和发送许可分别选择；仅有声音时，画面中的细节可能缺失。</p>
                </div></details> : selectedSource?.saved_visual_available && <details className="vaa-optional"><summary>已保存的画面（可选）</summary><label className="vaa-consent"><input type="checkbox" checked={useSavedVisuals} onChange={event => { setUseSavedVisuals(event.target.checked); if (!event.target.checked) setAllowModelImages(false) }}/><span>使用已保存的画面观察</span></label>{useSavedVisuals && <label className="vaa-consent"><input type="checkbox" checked={allowModelImages} onChange={event => setAllowModelImages(event.target.checked)}/><span>允许将这些画面发送给模型服务，补充本次分析</span></label>}</details>}
                <p className="vaa-hint">{helpLine}</p>
              </section>
              <div className="vaa-actions"><button className="vaa-primary" type="submit" disabled={Boolean(submitHint)}>{submitting ? '正在提交…' : actionLabel}</button><p className="vaa-hint" role="status">{submitHint || '准备好了，提交后可在下方查看进度。'}</p></div>
            </form>
          </details>
          {pendingId && <p className="vaa-message is-warning" role="status">提交结果待确认。页面会继续查询；确认前请不要重复提交。</p>}
          {(activeFlows.length > 0 || activeJobs.length > 0) && <section className="vaa-card" aria-label="正在处理"><h2>正在处理</h2>{activeFlows.map(flowRecord)}{activeJobs.map(jobRecord)}</section>}
          <section id="vaa-result" className="vaa-result" aria-label="当前结果">
            {selected && candidateLoading && <div className="vaa-card" role="status">正在读取所选内容…</div>}
            {selected && candidateError && <div className="vaa-message is-error" role="alert">{candidateError} <button type="button" className="vaa-secondary" onClick={() => void readCandidate(selected, true)}>重新读取</button></div>}
            {selectedJob ? <>
              <header className="vaa-result-head"><div><p className="vaa-hint">当前{contentLabel(selectedJob)} · v{selectedJob.revision?.version || 1}</p><h2>{selectedTitle}</h2></div><div className="vaa-result-actions">
                {selectedJob.source_id && sources.some(item => item.source_id === selectedJob.source_id) && <button type="button" className="vaa-secondary" disabled={submitting || Boolean(pendingId)} onClick={askAboutSelected}>继续提问</button>}
                {canRevise && <button type="button" className="vaa-secondary" onClick={openEditor}>修改内容</button>}
                <button type="button" className="vaa-primary" onClick={() => void downloadJob(selectedJob)}>下载</button>
              </div></header>
              <details className="vaa-hint"><summary>查看本次要求</summary><p>{selectedJob.user_request}</p></details>
              {selectedJob.solution_status && selectedJob.solution_status !== 'ready' && <div className="vaa-message is-warning" role="status"><strong>{selectedJob.solution_status === 'unsupported' ? '这次要求暂时无法完成' : selectedJob.solution_status === 'needs_clarification' ? '还需要确认一件事' : '目前只能完成一部分'}</strong>{selectedJob.solution_gaps?.map((gap, index) => <p key={index}>{gap.description}</p>)}</div>}
              {selectedJob.revision_note && !explanations.length && selectedJob.result?.markdown.includes('/bridge/assets/') && <p className="vaa-hint">插图未随文字修改更新。</p>}
              {selectedJob.vision_note && <p role="status" className="vaa-hint">{selectedJob.vision_note}</p>}
              {['not_authorized', 'frames_unavailable', 'outcome_unknown', 'invalid_visual_reply', 'rejected'].includes(selectedJob.vision_state || '') && !selectedJob.vision_note && <p role="status" className="vaa-hint">本次内容仅依据声音材料，画面中的细节可能缺失。</p>}
              <p className="vaa-hint">识别与整理可能有误，重要细节请核对。</p>
              <div className={`vaa-reading-workspace${conversationOpen ? ' is-chat-open' : ''}${conversationOpen && conversationMode === 'full' ? ' is-chat-full' : ''}`}>
              <div className="vaa-reading-column">
              {reader}
              <SourceReference key={selectedJob.job_id} segments={selectedJob.result?.transcript?.segments || []} sourceUrl={selectedJob.source_url}/>
              {canRevise && <details id="vaa-editor" className="vaa-card vaa-editor"><summary>修改这份内容</summary><form onSubmit={reviseSelectedJob}>
                <p className="vaa-hint">修改会另存为新版本，旧稿保留。</p>
                {selectedJob.local_edits_available && <fieldset className="vaa-choice-row" disabled={submitting || Boolean(pendingId)}><legend>修改方式</legend><label><input type="radio" name="revision-mode" checked={revisionMode === 'local'} onChange={() => {setRevisionMode('local'); setAllowRevision(false)}}/>直接修改</label><label><input type="radio" name="revision-mode" checked={revisionMode === 'model'} onChange={() => {setRevisionMode('model'); setAllowRevision(false)}}/>请模型修改</label></fieldset>}
                {selectedJob.local_edits_available && revisionMode === 'local' ? <><p className="vaa-hint">直接修改不调用模型。复制要修改的一段原文，再填写替换内容。</p><label className="vaa-field"><span>原文</span><textarea rows={4} value={editBefore} onChange={event => {setEditBefore(event.target.value); setError('')}} placeholder="从正文复制一段原文（1～4000字符）"/></label><label className="vaa-field"><span>改为</span><textarea rows={4} value={editAfter} onChange={event => {setEditAfter(event.target.value); setError('')}} placeholder="输入替换内容；留空表示删除（最多8000字符）"/></label></> : <><label className="vaa-field"><span>修改要求</span><textarea rows={3} value={revisionInstruction} onChange={event => {setRevisionInstruction(event.target.value); setAllowRevision(false)}} required placeholder="写下这次需要调整的内容"/></label><label className="vaa-consent"><input type="checkbox" checked={allowRevision} onChange={event => setAllowRevision(event.target.checked)}/><span>本次允许将修改要求和成果文字发送给模型服务</span></label></>}
                <button className="vaa-primary" type="submit" disabled={submitting || Boolean(pendingId) || activeStates.has(selectedJob.state) || (revisionMode === 'model' && (!revisionInstruction.trim() || !allowRevision))}>保存修改稿</button>
              </form></details>}
              </div>
              {selectedJob.source_id && selectedJob.revision?.record_fingerprint && sources.some(item => item.source_id === selectedJob.source_id) && <PurposeConversation
                key={`${selectedJob.job_id}:${selectedJob.revision.record_fingerprint}`} basis={selectedJob} jobs={jobs}
                visible={conversationOpen} mode={conversationMode} onModeChange={setConversationMode} onClose={() => setConversationOpen(false)}
                blocked={!connected || submitting || Boolean(pendingId) || activeJobs.length > 0 || activeFlows.length > 0}
                submit={submitQuestion} read={id => api<ConversationAnswer>(`outputs/${encodeURIComponent(id)}`)} cancel={cancelQuestion} onOpenSaved={chooseJob}/>} 
              </div>
            </> : !candidateLoading && !candidateError && <section className="vaa-card vaa-empty"><h2>从一个视频或问题开始</h2><p>完成处理后，内容会出现在这里；已保存内容也可以从左侧打开。</p></section>}
          </section>
          <details className="vaa-card vaa-activity"><summary>全部处理记录{stateLoaded ? `（${flowRows.length + jobs.length}）` : ''}</summary>
            {!stateLoaded && <p role="status">正在读取记录…</p>}
            {stateLoaded && flowRows.length === 0 && jobs.length === 0 && <p className="vaa-hint">还没有处理记录。</p>}
            {[...flowRows].reverse().map(flowRecord)}{[...jobs].reverse().map(jobRecord)}
            {(flowId || pendingId) && <details className="vaa-hint"><summary>本次请求编号</summary>{lastRequestId && <p>request_id：{lastRequestId}</p>}{flowId && <p>flow_id：{flowId}</p>}{pendingId && <p>待确认编号：{pendingId}</p>}</details>}
          </details>
        </>}
      </main>
    </div>
  </div>
}
createRoot(document.getElementById('root')!).render(<PurposeBridge/>)
export default PurposeBridge


