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
  vision_state?: string | null
  vision_note?: string | null
}

type BridgeState = {scope_id?: string; flows: Flow[]; jobs: Job[]}

class BridgeApiError extends Error {
  code?: string
  status?: number
  admission?: string
}

function errorMessage(cause: unknown, fallback: string) {
  return cause instanceof Error ? cause.message : fallback
}

const SELECTED_KEY = 'vaa-video-notes-selected-v1'
const PENDING_KEY = 'vaa-video-notes-pending-v1'
const activeFlowStates = new Set(['queued', 'extracting', 'building'])

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
  if (flow.state === 'extracting') return '正在读取视频内容'
  if (flow.state === 'building') return '正在整理笔记'
  if (flow.state === 'available') return '笔记已保存，可阅读'
  if (flow.state === 'failed') return '处理失败'
  if (flow.state === 'interrupted') return '处理已中断，结果待确认'
  if (flow.state === 'canceled') return '已取消'
  return '状态待确认'
}

function VideoNotesBridge() {
  const [connected, setConnected] = useState(false)
  const [flows, setFlows] = useState<Flow[]>([])
  const [jobs, setJobs] = useState<Job[]>([])
  const [source, setSource] = useState('')
  const [request, setRequest] = useState('')
  const [allowLocalMedia, setAllowLocalMedia] = useState(false)
  const [allowModelMaterials, setAllowModelMaterials] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [selected, setSelected] = useState(() => sessionStorage.getItem(SELECTED_KEY) || '')
  const [flowId, setFlowId] = useState('')
  const [lastRequestId, setLastRequestId] = useState(() => sessionStorage.getItem(PENDING_KEY) || '')
  const [uncertainRequestId, setUncertainRequestId] = useState(() => sessionStorage.getItem(PENDING_KEY) || '')
  const [error, setError] = useState('')
  const [canceling, setCanceling] = useState('')
  const polling = useRef(false)

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
    if (!allowLocalMedia || !allowModelMaterials || submitting || uncertainRequestId) return
    const requestId = crypto.randomUUID()
    setLastRequestId(requestId)
    setFlowId('')
    sessionStorage.setItem(PENDING_KEY, requestId)
    setSubmitting(true)
    setError('')
    setAllowLocalMedia(false)
    setAllowModelMaterials(false)
    try {
      const response = await api<{flow_id: string}>('flows', {
        method: 'POST',
        body: JSON.stringify({
          request_id: requestId,
          source,
          user_request: request,
          output_kind: 'structured_notes',
          allow_local_audio_read: true,
          allow_local_video_read: true,
          allow_model_text: true,
          allow_model_images: true,
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

  async function downloadNote(job: Job) {
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(job.job_id)) {
      setError('笔记编号无效，无法下载。')
      return
    }
    setError('')
    try {
      const response = await fetch(`/api/bridge/notes/${job.job_id}/download`, {
        credentials: 'same-origin',
        cache: 'no-store',
      })
      if (!response.ok) {
        const value = await response.json().catch(() => ({})) as Record<string, unknown>
        const detail = typeof value.detail === 'string' ? value.detail : `下载失败（HTTP ${response.status}）`
        throw new Error(detail)
      }
      const disposition = response.headers.get('Content-Disposition') || ''
      const declaredName = disposition.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i)?.[1]
      const filename = declaredName === 'notes.zip' ? 'notes.zip' : 'notes.md'
      const url = URL.createObjectURL(await response.blob())
      const link = document.createElement('a')
      link.href = url
      link.download = filename
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch (cause: unknown) {
      setError(errorMessage(cause, '笔记下载失败'))
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

  return <>
    <Toaster/>
    <style>{`.vaa-video-notes-reader button:has(svg.lucide-message-square),.vaa-video-notes-reader button:has(svg.lucide-download){display:none}`}</style>
    <main className="mx-auto max-w-5xl space-y-5 px-6 py-6">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">视频笔记</h1>
        <p className="text-sm text-slate-600">粘贴公开视频链接，写下你的整理要求。自动整理可能存在识别错误，重要细节请核对。</p>
      </header>

      {connected ? <form onSubmit={submit} className="grid gap-3 rounded-lg border bg-white p-4">
        <label className="grid gap-1 text-sm">
          <span>公开视频链接</span>
          <input className="rounded-md border p-2" type="url" value={source} onChange={event => {
            setSource(event.target.value)
            setAllowLocalMedia(false)
            setAllowModelMaterials(false)
          }} required placeholder="粘贴视频链接"/>
        </label>
        <label className="grid gap-1 text-sm">
          <span>本次整理要求</span>
          <textarea className="rounded-md border p-2" rows={3} value={request} onChange={event => {
            setRequest(event.target.value)
            setAllowLocalMedia(false)
            setAllowModelMaterials(false)
          }} required placeholder="写下希望整理的内容、结构或边界"/>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={allowLocalMedia} onChange={event => setAllowLocalMedia(event.target.checked)}/>
          <span>允许在本机读取此视频必要的音轨和画面</span>
        </label>
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" checked={allowModelMaterials} onChange={event => setAllowModelMaterials(event.target.checked)}/>
          <span>允许把必要的文字和选取的画面发送给配置的模型整理笔记</span>
        </label>
        <button className="w-fit rounded-md bg-blue-600 px-4 py-2 text-white disabled:opacity-40" disabled={!source.trim() || !request.trim() || !allowLocalMedia || !allowModelMaterials || submitting || Boolean(uncertainRequestId)} type="submit">
          {submitting ? '正在提交…' : '生成笔记'}
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
        <span>已保存的笔记</span>
        <select className="rounded-md border p-2" aria-label="已保存成果" value={selectedJob?.job_id || ''} onChange={event => {
          const next = event.target.value
          setSelected(next)
          sessionStorage.setItem(SELECTED_KEY, next)
          useTaskStore.setState({currentTaskId: next})
        }}>
          {readableJobs.map(job => <option key={job.job_id} value={job.job_id}>{job.bvid || job.source_url}{job.page ? ` · P${job.page}` : ''}</option>)}
        </select>
      </label>}
    </main>

    {connected && selectedJob && <>
      <section className="mx-auto flex max-w-5xl flex-wrap items-center gap-3 px-6 pb-3 text-sm">
        <button type="button" className="rounded-md border px-3 py-2 hover:bg-slate-50" onClick={() => void downloadNote(selectedJob)}>下载笔记</button>
        {selectedJob.vision_note && <p role="status" className="text-sm text-slate-600">{selectedJob.vision_note}</p>}
        {['not_authorized', 'frames_unavailable', 'outcome_unknown', 'invalid_visual_reply', 'rejected'].includes(selectedJob.vision_state || '') && !selectedJob.vision_note && <p role="status" className="text-sm text-slate-600">画面材料未能补入，本次笔记仅依据声音内容；画面中的细节可能缺失。</p>}
      </section>
      <div className="vaa-video-notes-reader min-h-screen">
        <MarkdownViewer status="success" content=""/>
      </div>
    </>}
  </>
}

createRoot(document.getElementById('root')!).render(<VideoNotesBridge/>)

export default VideoNotesBridge
