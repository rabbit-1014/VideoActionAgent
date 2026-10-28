import React, {useEffect, useState} from 'react'
import {createRoot} from 'react-dom/client'
import {Toaster} from 'react-hot-toast'
import MarkdownViewer from './pages/HomePage/components/MarkdownViewer'
import {useTaskStore} from './store/taskStore'
import './index.css'

async function api(path: string, options: RequestInit = {}) {
  const response = await fetch('/api/bridge/' + path, {...options,
    headers: {'Content-Type': 'application/json'}, cache: 'no-store'})
  const value = await response.json()
  if (!response.ok) throw new Error(value.detail || '操作未成功')
  return value
}

function Bridge() {
  const [connected, setConnected] = useState(false)
  const [masters, setMasters] = useState<any[]>([])
  const [jobs, setJobs] = useState<any[]>([])
  const [master, setMaster] = useState('short')
  const [request, setRequest] = useState('整理视频讲解的字幕方法，包含适用情况、操作步骤和注意事项；不补材料未给出的现行界面或画面细节。')
  const [permit, setPermit] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [selected, setSelected] = useState(sessionStorage.getItem('vaa-native-selected') || '')

  async function refresh() {
    const value = await api('state')
    setMasters(value.masters); setJobs(value.jobs)
    const available = value.jobs.filter((row: any) => row.state === 'available')
    const tasks = available.map((row: any) => ({id: row.request_id, status: 'SUCCESS',
      markdown: row.result.markdown, transcript: row.result.transcript,
      audioMeta: row.result.audio_meta, createdAt: row.created_at || '',
      formData: {model_name: 'glm-5.3', style: row.style, provider_id: 'env-zhipu',
        video_url: row.source_url, platform: 'bilibili', screenshot: false, link: false},
    }))
    const chosen = tasks.some((t: any) => t.id === selected) ? selected : tasks.at(-1)?.id || ''
    useTaskStore.setState({tasks, currentTaskId: chosen} as any)
    sessionStorage.setItem('vaa-native-selected', chosen)
    if (chosen !== selected) setSelected(chosen)
  }

  useEffect(() => {
    const token = new URLSearchParams(location.hash.slice(1)).get('token')
    if (token) {
      history.replaceState(null, '', location.pathname)
      api('session', {method: 'POST', body: JSON.stringify({token})})
        .then(() => setConnected(true)).catch(e => setError(e.message))
    } else api('state').then(() => setConnected(true)).catch(e => setError(e.message))
  }, [])
  useEffect(() => {
    if (!connected) return
    refresh().catch(e => setError(e.message))
    const timer = setInterval(() => refresh().catch(e => setError(e.message)), 2000)
    return () => clearInterval(timer)
  }, [connected, selected])

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (!permit || busy) return
    const payload = {request_id: crypto.randomUUID(), master_id: master,
      user_request: request, allow_model_text: true, style: 'tutorial'}
    setBusy(true); setError(''); setPermit(false)
    try {
      await api('notes', {method: 'POST', body: JSON.stringify(payload)})
      await refresh()
    } catch (e: any) {
      setError(e.message + '；未自动重发，请从下方记录核对。请求编号：' + payload.request_id)
    } finally { setBusy(false) }
  }
  const available = jobs.filter(row => row.state === 'available')
  const label: Record<string,string> = {admitted:'等待生成',model_dispatched:'正在生成',reply_received:'正在保存',available:'已保存',cancelled:'已取消',model_failed_or_unknown:'模型调用失败或结果未确认',native_failed_or_unconfirmed:'原生结果未确认',native_admission_failed:'生成尚未开始',native_result_invalid:'成果不可读取',native_result_not_published:'成果未发布'}
  return <><Toaster/>
  <style>{`.vaa-native-reader button:has(svg.lucide-message-square){display:none}`}</style>
  <header className="mx-auto max-w-5xl space-y-3 px-6 py-5">
    <h1 className="text-2xl font-semibold">视频内容 → 学习笔记</h1>
    <p className="text-sm text-slate-600">使用已保存的视频内容，沿 BiliNote 原生流程整理。结果为候选，仍需核对。</p>
    {connected && <form onSubmit={submit} className="grid gap-3">
      <select className="rounded-md border p-2" aria-label="选择母版" value={master} onChange={e => setMaster(e.target.value)}>
        {masters.map(m => <option key={m.id} value={m.id}>{m.title}</option>)}
      </select>
      <textarea className="rounded-md border p-2" aria-label="本次要求" rows={3} value={request} onChange={e => setRequest(e.target.value)} required/>
      <label><input type="checkbox" checked={permit} onChange={e => setPermit(e.target.checked)}/>
        允许将这份已保存母版的文字发送给配置模型，生成笔记</label>
      <button className="w-fit rounded-md bg-blue-600 px-4 py-2 text-white disabled:opacity-40" disabled={!permit || busy} type="submit">生成原生笔记</button>
    </form>}
    <p role="status">{error}</p>
    <div className="space-y-2">{jobs.map(row => <div key={row.request_id} className="flex flex-wrap items-center gap-3 text-sm">
      {masters.find(m=>m.id===row.master_id)?.title} · {label[row.state] || '状态未确认'}
      {row.state === 'available' && <button className="text-blue-700 underline" onClick={() => setSelected(row.request_id)}>打开此稿</button>}
      {['admitted','model_dispatched','reply_received'].includes(row.state) && <button onClick={() => api('cancel',{method:'POST',body:JSON.stringify({request_id:row.request_id})}).then(refresh).catch(e=>setError(e.message))}>取消生成</button>}
      <details><summary>请求记录</summary>{row.request_id}</details>
    </div>)}</div>
    {available.length > 0 && <select className="rounded-md border p-2" aria-label="已保存成果" value={selected} onChange={e => setSelected(e.target.value)}>
      {available.map(row => <option key={row.request_id} value={row.request_id}>{masters.find(m=>m.id===row.master_id)?.title}</option>)}
    </select>}
  </header>
  {connected && selected && <div className="vaa-native-reader"><MarkdownViewer status="success" content=""/></div>}
  </>
}
createRoot(document.getElementById('root')!).render(<Bridge/> )
