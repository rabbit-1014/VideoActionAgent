import React, {useCallback, useEffect, useRef, useState} from 'react'
import {createRoot} from 'react-dom/client'
import {Toaster} from 'react-hot-toast'
import MarkdownViewer from './pages/HomePage/components/MarkdownViewer'
import {useTaskStore, type Task} from './store/taskStore'
import './index.css'

type NativeNoteResult = {
  markdown: string
  transcript: Task['transcript']
  audio_meta: Task['audioMeta']
}

type Flow = {
  flow_id: string
  source_job_id?: string
  notes_job_id?: string
  bvid?: string
  page?: number
  user_request: string
  state: string
  source_state?: string
  notes_state?: string
  result_available?: boolean
  error_code?: string | null
}

type Job = {
  job_id: string
  request_id: string
  master_id?: string
  user_request: string
  state: string
  source_url: string
  created_at?: string
  result?: NativeNoteResult
  bvid?: string
  page?: number
  formal_task: false
  completed: false
  illustration_state?: IllustrationState
  illustration_id?: string
  image_count?: number
  illustration_error?: string
}

type IllustrationState = 'available' | 'not_needed' | 'unavailable'
type IllustrationAttemptState = 'pending' | IllustrationState
type IllustrationResponse = {state: IllustrationState; artifact_id?: string; image_count?: number; detail?: string}
type BridgeState = {scope_id?: string; flows: Flow[]; jobs: Job[]}

function loadIllustrationAttempts(): Record<string, IllustrationAttemptState> {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(ILLUSTRATION_ATTEMPTS_KEY) || '{}') as Record<string, unknown>
    return Object.fromEntries(Object.entries(parsed).flatMap(([jobId, value]) => {
      if (value === true || value === 'pending') return [[jobId, 'pending']]
      if (value === 'available' || value === 'not_needed' || value === 'unavailable') return [[jobId, value]]
      return []
    })) as Record<string, IllustrationAttemptState>
  } catch {
    return {}
  }
}

class BridgeApiError extends Error {
  code?: string
  status?: number
  admission?: string
}

function errorMessage(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback
}

const SELECTED_KEY = 'vaa-illustrated-native-selected-v1'
const PENDING_KEY = 'vaa-illustrated-native-pending-v1'
const activeFlowStates = new Set(['queued', 'extracting', 'building'])
const ILLUSTRATION_ATTEMPTS_KEY = 'vaa-illustrated-native-attempts-v1'

async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/bridge/${path}`, {
    ...options,
    credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', ...options.headers},
    cache: 'no-store',
  })
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

function flowLabel(flow: Flow) {
  if (flow.state === 'queued') return '排队中'
  if (flow.state === 'extracting') return '正在读取公开视频音轨并转写'
  if (flow.state === 'building') return '正在生成原生笔记'
  if (flow.state === 'available') return '笔记已保存，可阅读'
  if (flow.state === 'failed') return '处理失败'
  if (flow.state === 'interrupted') return '处理已中断，结果待确认'
  if (flow.state === 'canceled') return '已取消'
  return '状态待确认'
}

function IllustratedBridge() {
  const [connected, setConnected] = useState(false)
  const [flows, setFlows] = useState<Flow[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [source, setSource] = useState('')
  const [request, setRequest] = useState('')
  const [allowAudio, setAllowAudio] = useState(false)
  const [allowModel, setAllowModel] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [selected, setSelected] = useState(() => sessionStorage.getItem(SELECTED_KEY) || '')
  const [flowId, setFlowId] = useState('')
  const [lastRequestId, setLastRequestId] = useState(() => sessionStorage.getItem(PENDING_KEY) || '')
  const [uncertainRequestId, setUncertainRequestId] = useState(() => sessionStorage.getItem(PENDING_KEY) || '')
  const [error, setError] = useState('')
  const [canceling, setCanceling] = useState('')
  const [illustrationBusy, setIllustrationBusy] = useState(false)
  const [illustrationMessage, setIllustrationMessage] = useState('')
  const [illustrationAttempts, setIllustrationAttempts] = useState<Record<string, IllustrationAttemptState>>(loadIllustrationAttempts)
  const polling = useRef(false)
  const illustrationRequestStarted = useRef(new Set<string>())

  function setIllustrationAttempt(jobId: string, state?: IllustrationAttemptState) {
    const next = {...illustrationAttempts}
    if (state) next[jobId] = state
    else delete next[jobId]
    setIllustrationAttempts(next)
    sessionStorage.setItem(ILLUSTRATION_ATTEMPTS_KEY, JSON.stringify(next))
  }

  const refresh = useCallback(async () => {
    if (polling.current) return
    polling.current = true
    try {
      const value = await api<BridgeState>('state')
      const nextFlows = Array.isArray(value.flows) ? value.flows : []
      const nextJobs = Array.isArray(value.jobs) ? value.jobs : []
      setFlows(nextFlows)
      setJobs(nextJobs)

      if (uncertainRequestId && nextFlows.some(flow => flow.flow_id === uncertainRequestId)) {
        sessionStorage.removeItem(PENDING_KEY)
        setUncertainRequestId('')
        setError('请求状态已在记录中找到。')
      }

      const readable = nextJobs.filter(job => Boolean(job.result) && typeof job.result?.markdown === 'string')
      const tasks: (Task & {formData: Task['formData'] & {style: string}})[] = readable.map(job => ({
        id: job.job_id,
        status: 'SUCCESS' as const,
        markdown: job.result!.markdown.replaceAll('(/bridge/assets/', '(/api/bridge/assets/'),
        transcript: job.result!.transcript,
        audioMeta: job.result!.audio_meta || {},
        createdAt: job.created_at || '',
        formData: {
          model_name: 'glm-5.3',
          style: 'tutorial',
          provider_id: 'env-zhipu',
          video_url: job.source_url,
          platform: 'bilibili',
          quality: '',
          screenshot: false,
          link: false,
        },
      }))
      const chosen = tasks.some(task => task.id === selected) ? selected : tasks.at(-1)?.id || ''
      useTaskStore.setState({tasks, currentTaskId: chosen})
      sessionStorage.setItem(SELECTED_KEY, chosen)
      if (chosen !== selected) setSelected(chosen)
    } finally {
      polling.current = false
    }
  }, [selected, uncertainRequestId])

  useEffect(() => {
    let alive = true
    const token = new URLSearchParams(location.hash.slice(1)).get('token')
    if (token) history.replaceState(null, '', `${location.pathname}${location.search}`)
    const start = token
      ? api('session', {method: 'POST', body: JSON.stringify({token})})
      : api('state')
    start.then(() => { if (alive) setConnected(true) })
      .catch((cause: unknown) => { if (alive) setError(errorMessage(cause, '连接本机服务失败')) })
    return () => { alive = false }
  }, [])

  useEffect(() => {
    if (!connected) return
    let timer: number | undefined
    let alive = true
    const tick = async () => {
      try { await refresh() }
      catch (cause: unknown) { if (alive) setError(errorMessage(cause, '读取状态失败')) }
      if (alive) timer = window.setTimeout(tick, 2000)
    }
    void tick()
    return () => { alive = false; if (timer) window.clearTimeout(timer) }
  }, [connected, refresh])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!allowAudio || !allowModel || submitting || uncertainRequestId) return
    const requestId = crypto.randomUUID()
    setLastRequestId(requestId)
    setFlowId('')
    sessionStorage.setItem(PENDING_KEY, requestId)
    setSubmitting(true)
    setError('')
    setAllowAudio(false)
    setAllowModel(false)
    try {
      const response = await api<{flow_id: string}>('flows', {
        method: 'POST',
        body: JSON.stringify({
          request_id: requestId,
          source,
          user_request: request,
          output_kind: 'structured_notes',
          allow_local_audio_read: true,
          allow_model_text: true,
        }),
      })
      if (!response.flow_id) throw new Error('服务未返回 flow_id，请查询请求记录确认状态。')
      setFlowId(response.flow_id)
      sessionStorage.removeItem(PENDING_KEY)
      setUncertainRequestId('')
      try { await refresh() } catch { setError('请求已确认接收，但暂时无法更新状态；页面会继续只读查询。') }
    } catch (cause: unknown) {
      if (cause instanceof BridgeApiError && (cause.admission === 'rejected_before_create' || cause.status === 422)) {
        sessionStorage.removeItem(PENDING_KEY)
        setUncertainRequestId('')
        setError(`${errorMessage(cause, '请求未创建')}；可修改后手动提交。`)
      } else {
        setUncertainRequestId(requestId)
        setError(`${errorMessage(cause, '提交结果未确认')}；不会自动重发，请查看请求记录并等待状态查询。`)
      }
    } finally {
      setSubmitting(false)
    }
  }

  async function addIllustrations(job: Job) {
    if (illustrationBusy || illustrationRequestStarted.current.has(job.job_id) || illustrationAttempts[job.job_id] || job.illustration_state) return
    illustrationRequestStarted.current.add(job.job_id)
    setIllustrationAttempt(job.job_id, 'pending')
    setIllustrationBusy(true)
    setIllustrationMessage('正在登记已取得的相关画面…')
    setError('')
    let response: IllustrationResponse
    try {
      response = await api<IllustrationResponse>('illustrations', {
        method: 'POST',
        body: JSON.stringify({notes_job_id: job.job_id, allow_local_images: true}),
      })
    } catch (cause: unknown) {
      const rejectedBeforeCreate = cause instanceof BridgeApiError && (
        cause.status === 422 ||
        cause.admission === 'rejected_before_create' ||
        (cause.status === 403 && /permission|consent|local_images|allow|许可|授权/i.test(cause.code || cause.message))
      )
      if (rejectedBeforeCreate) {
        illustrationRequestStarted.current.delete(job.job_id)
        setIllustrationAttempt(job.job_id)
        setIllustrationMessage('请求在创建前被拒绝；修正许可或输入后可手动重试。')
      } else {
        setIllustrationMessage('插图结果待确认；页面会继续只读查询，不会自动重发。')
      }
      setError(errorMessage(cause, '插图请求结果未确认'))
      try { await refresh() } catch { /* status polling will retry read-only */ }
      setIllustrationBusy(false)
      return
    }

    if (response.state === 'available' || response.state === 'not_needed' || response.state === 'unavailable') {
      setIllustrationAttempt(job.job_id, response.state)
      if (response.state === 'available') {
        setIllustrationMessage('已加入 ' + (response.image_count || 0) + ' 张相关截图。')
      } else if (response.state === 'not_needed') {
        setIllustrationMessage('暂无适合加入的截图，保留文字内容。')
      } else {
        setIllustrationMessage('当前没有可用截图材料，保留文字内容。')
      }
      try { await refresh() } catch {
        setIllustrationMessage('插图状态已记录；笔记内容将在状态刷新后显示。')
      }
    } else {
      setIllustrationAttempt(job.job_id, 'pending')
      setIllustrationMessage('插图结果待确认；页面会继续只读查询，不会自动重发。')
    }
    setIllustrationBusy(false)
  }
  async function downloadIllustratedNote(artifactId: string) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(artifactId)) {
      setError('插图成果编号无效，无法下载。')
      return
    }
    setIllustrationBusy(true)
    setError('')
    try {
      const response = await fetch('/api/bridge/illustrations/' + artifactId + '/download', {
        credentials: 'same-origin',
        cache: 'no-store',
      })
      if (!response.ok) {
        const value = await response.json().catch(() => ({})) as Record<string, unknown>
        const detail = typeof value.detail === 'string' ? value.detail : '下载失败（HTTP ' + response.status + '）'
        throw new Error(detail)
      }
      const blob = await response.blob()
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'illustrated-notes-' + selected + '.zip'
      document.body.appendChild(link)
      link.click()
      document.body.removeChild(link)
      URL.revokeObjectURL(url)
    } catch (cause: unknown) {
      setError(errorMessage(cause, '含图片笔记下载失败'))
    } finally {
      setIllustrationBusy(false)
    }
  }
  async function cancel(flow: Flow) {
    setCanceling(flow.flow_id)
    setError('')
    try {
      await api('cancel', {method: 'POST', body: JSON.stringify({flow_id: flow.flow_id})})
      await refresh()
    } catch (cause: unknown) {
      setError(`${errorMessage(cause, '取消结果未确认')}；请查看状态记录。`)
    } finally {
      setCanceling('')
    }
  }

  const readableJobs = jobs.filter(job => Boolean(job.result) && typeof job.result?.markdown === 'string')
  const selectedJob = readableJobs.find(job => job.job_id === selected)
  const selectedIllustrationState = selectedJob?.illustration_state ?? (selectedJob ? illustrationAttempts[selectedJob.job_id] : undefined)

  return <>
    <Toaster/>
    <style>{`.vaa-illustrated-native-reader button:has(svg.lucide-message-square){display:none}`}</style>
    <main className="mx-auto max-w-5xl space-y-5 px-6 py-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">视频笔记</h1>
        <p className="text-sm text-slate-600">提交后在本机读取公开视频音轨并整理笔记。笔记是待核对的候选成果，不代表正式任务完成。</p>
      </header>

      {connected ? <form onSubmit={submit} className="grid gap-3 rounded-lg border bg-white p-4">
        <label className="grid gap-1 text-sm">
          <span>公开视频链接</span>
          <input className="rounded-md border p-2" type="url" value={source} onChange={event => {
            setSource(event.target.value)
            setAllowAudio(false)
            setAllowModel(false)
          }} required placeholder="粘贴视频链接"/>
        </label>
        <label className="grid gap-1 text-sm">
          <span>本次整理要求</span>
          <textarea className="rounded-md border p-2" rows={3} value={request} onChange={event => {
            setRequest(event.target.value)
            setAllowAudio(false)
            setAllowModel(false)
          }} required placeholder="写下希望整理的内容、结构或边界"/>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={allowAudio} onChange={event => setAllowAudio(event.target.checked)}/>
          <span>允许在本机读取并转写此视频的音轨</span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={allowModel} onChange={event => setAllowModel(event.target.checked)}/>
          <span>允许把转写文字发送给配置的模型生成笔记</span>
        </label>
        <button className="w-fit rounded-md bg-blue-600 px-4 py-2 text-white disabled:opacity-40" disabled={!source.trim() || !request.trim() || !allowAudio || !allowModel || submitting || Boolean(uncertainRequestId)} type="submit">
          {submitting ? '正在提交…' : '生成原生笔记'}
        </button>
        {uncertainRequestId && <p className="text-sm text-amber-800">提交结果待确认。页面会继续只读查询；确认前暂不接受新提交。</p>}
      </form> : <p role="status" className="text-sm text-slate-600">{error || '正在连接本机服务…'}</p>}

      {error && connected && <p role="alert" className="text-sm text-red-700">{error}</p>}

      {(flowId || uncertainRequestId) && <details className="text-xs text-slate-600">
        <summary>请求记录与调试信息</summary>
      {lastRequestId && <p>request_id：{lastRequestId}</p>}
      {flowId && <p>flow_id：{flowId}</p>}
        {uncertainRequestId && <p>request_id（待查）：{uncertainRequestId}</p>}
      </details>}

      <section aria-label="处理记录" className="space-y-2">
        <h2 className="text-lg font-medium">处理记录</h2>
        {flows.length === 0 && <p className="text-sm text-slate-500">暂无记录</p>}
        {flows.map(flow => <article key={flow.flow_id} className="space-y-2 rounded-lg border bg-white p-3">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
            <span className="font-medium">{flowLabel(flow)}</span>
            {flow.bvid && <span>{flow.bvid}{flow.page ? ` · P${flow.page}` : ''}</span>}
            {flow.result_available && <span className="text-emerald-700">成果可阅读</span>}
            {activeFlowStates.has(flow.state) && <button className="text-blue-700 underline disabled:opacity-50" disabled={canceling === flow.flow_id} onClick={() => void cancel(flow)}>
              {canceling === flow.flow_id ? '正在取消…' : '取消'}
            </button>}
          </div>
          <p className="text-sm text-slate-600">{flow.user_request}</p>
          <details className="text-xs text-slate-500">
            <summary>流程详情</summary>
            <p>flow_id：{flow.flow_id}</p>
            <p>来源状态：{flow.source_state || '未提供'} · 笔记状态：{flow.notes_state || '未提供'}</p>
            {flow.error_code && <p>error_code：{flow.error_code}</p>}
          </details>
        </article>)}
      </section>

      {readableJobs.length > 0 && <label className="grid max-w-xl gap-1 text-sm">
        <span>已保存的原生笔记</span>
        <select className="rounded-md border p-2" aria-label="已保存成果" value={selectedJob?.job_id || ''} onChange={event => {
          const next = event.target.value
          setSelected(next)
          setIllustrationMessage('')
          sessionStorage.setItem(SELECTED_KEY, next)
          useTaskStore.setState({currentTaskId: next})
        }}>
          {readableJobs.map(job => <option key={job.job_id} value={job.job_id}>{job.bvid || job.source_url}{job.page ? ` · P${job.page}` : ''}</option>)}
        </select>
      </label>}
    </main>

    {connected && selectedJob && <>
      <section className="mx-auto flex max-w-5xl flex-wrap items-center gap-3 px-6 pb-4 text-sm">
        {selectedIllustrationState === 'available' && selectedJob.illustration_id && <button
          type="button"
          className="rounded-md border border-blue-600 px-3 py-2 text-blue-700 hover:bg-blue-50 disabled:opacity-50"
          disabled={illustrationBusy}
          onClick={() => void downloadIllustratedNote(selectedJob.illustration_id!)}
        >下载笔记</button>}
        {selectedIllustrationState !== 'available' && !selectedIllustrationState && <button
          type="button"
          className="rounded-md border px-3 py-2 hover:bg-slate-50 disabled:opacity-50"
          disabled={illustrationBusy}
          onClick={() => void addIllustrations(selectedJob)}
        >加入相关截图（可选）</button>}
        {!selectedIllustrationState && <span className="text-xs text-slate-500">点击即同意本机登记已取得的相关画面；不会自动抓帧、上传图片或重新生成笔记。仅有匹配材料时才会加入。</span>}
        {selectedIllustrationState === 'available' && <span className="text-emerald-700">已加入 {selectedJob.image_count || 0} 张相关截图</span>}
        {selectedIllustrationState === 'not_needed' && <span className="text-slate-600">暂无适合加入的截图，保留文字内容。</span>}
        {selectedIllustrationState === 'unavailable' && <span className="text-slate-600">当前没有可用截图材料，保留文字内容。</span>}
        {selectedIllustrationState === 'pending' && <span role="status" className="text-amber-800">插图请求已发出，正在确认状态；不会自动重发。</span>}
        {illustrationMessage && <span role="status" className="text-slate-600">{illustrationMessage}</span>}
      </section>
      <div className="vaa-illustrated-native-reader min-h-screen">
        <MarkdownViewer status="success" content=""/>
      </div>
    </>}
  </>
}

createRoot(document.getElementById('root')!).render(<IllustratedBridge/>)

export default IllustratedBridge








