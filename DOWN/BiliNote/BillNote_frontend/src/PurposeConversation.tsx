import {useEffect, useRef, useState} from 'react'
import ChatPanelView, {type ChatMode, type ChatViewMessage} from './pages/HomePage/components/ChatPanelView'
import {ConversationSession, type ConversationAnswer, type ConversationAdmission, type ConversationState} from './purposeConversationSession'

type Props = {
  basis: ConversationAnswer
  jobs: {job_id: string; state: string; error_code?: string | null}[]
  blocked: boolean; visible: boolean; mode: ChatMode
  submit: (id: string, question: string, parent: ConversationAnswer, basis: ConversationAnswer) => Promise<ConversationAdmission>
  read: (id: string) => Promise<ConversationAnswer>
  cancel: (id: string) => Promise<void>
  onOpenSaved: (id: string) => void
  onModeChange: (mode: ChatMode) => void
  onClose: () => void
}

export default function PurposeConversation(props: Props) {
  const {basis, jobs, blocked, visible, mode, onModeChange, onClose, onOpenSaved} = props
  const ports = useRef(props)
  ports.current = props
  const session = useRef<ConversationSession | null>(null)
  const [state, setState] = useState<ConversationState>({head: basis, pending: null, error: '', readFailed: false})
  const [input, setInput] = useState('')
  const [authorized, setAuthorized] = useState(false)
  const [canceling, setCanceling] = useState(false)
  const [cancelError, setCancelError] = useState('')
  const sentDraft = useRef<{id: string; value: string} | null>(null)
  useEffect(() => {
    const controller = new ConversationSession(basis, {
      submit: (id, question, parent) => ports.current.submit(id, question, parent, basis),
      read: id => ports.current.read(id),
    }, setState)
    session.current = controller
    setState(controller.state)
    return () => {controller.dispose(); if (session.current === controller) session.current = null}
  }, [basis])
  useEffect(() => {void session.current?.observe(jobs)}, [jobs])
  useEffect(() => {
    const sent = sentDraft.current
    if (!sent) return
    if ((state.pending?.id === sent.id && state.pending.phase !== 'submitting') || state.head.job_id === sent.id) {
      setInput(current => current === sent.value ? '' : current)
      sentDraft.current = null
    } else if (!state.pending && state.error) {
      // A pre-creation refusal keeps the question editable. Never restore over a newer draft.
      sentDraft.current = null
    }
  }, [state])
  useEffect(() => {
    if (!visible) return
    const timer = window.setTimeout(() => {
      const field = document.querySelector<HTMLTextAreaElement>('.vaa-conversation textarea')
      field?.focus({preventScroll: true})
      field?.closest('aside')?.scrollIntoView({block: 'nearest'})
    }, 0)
    return () => window.clearTimeout(timer)
  }, [visible])

  function send(value: string) {
    if (blocked || !authorized || !value.trim() || state.pending) return
    const controller = session.current
    if (!controller || controller.state.pending) return
    const id = crypto.randomUUID()
    sentDraft.current = {id, value}
    const attempt = controller.send(id, value, authorized)
    setAuthorized(false); setCancelError('')
    void attempt
  }
  async function cancel() {
    const id = state.pending?.id
    if (!id || canceling) return
    setCanceling(true); setCancelError('')
    try {await ports.current.cancel(id)}
    catch {setCancelError('取消结果尚未确认，请查看处理记录。')}
    finally {setCanceling(false)}
  }

  const head = state.head
  const turns = [...(head.conversation_turns || []), {job_id: head.job_id, user_request: head.user_request, markdown: head.result?.markdown || ''}]
  const messages: ChatViewMessage[] = turns.slice(1).flatMap(turn => [
    {id: `${turn.job_id}-q`, role: 'user', content: turn.user_request},
    {id: `${turn.job_id}-a`, role: 'assistant', content: turn.markdown,
      footer: <button type="button" className="vaa-chat-open" onClick={() => onOpenSaved(turn.job_id)}>打开已保存内容</button>},
  ])
  if (state.pending) messages.push({id: `${state.pending.id}-q`, role: 'user', content: state.pending.question})
  const canCancel = Boolean(state.pending && jobs.some(job => job.job_id === state.pending!.id && ['queued', 'running'].includes(job.state)))
  const loading = Boolean(state.pending && !state.readFailed && state.pending.phase !== 'unknown')
  return <aside className="vaa-conversation" hidden={!visible} aria-label="视频问答">
    <ChatPanelView title="视频问答" messages={messages} input={input} visible={visible}
      onInputChange={value => {setInput(value); setAuthorized(false)}} onSend={send}
      loading={loading} disabled={blocked && !state.pending} canSend={authorized && !blocked && !state.pending}
      onCancel={canCancel && !canceling ? () => void cancel() : undefined}
      mode={mode} onModeChange={onModeChange} onClose={onClose}
      empty={<div><p>在这里继续提问</p><p className="mt-2 text-xs">会参考当前内容和已有对话，答复会自动保存。</p></div>}
      footer={<div className="vaa-chat-controls">
        {state.pending && <p role="status">{state.pending.phase === 'unknown' ? '提交结果待确认，正在查询记录；不会重复提交。' : state.readFailed ? '答复已保存，尚未读取成功。' : state.pending.phase === 'reading' ? '正在读取答复…' : '正在处理本次问题…'}</p>}
        {(state.error || cancelError) && <p role="alert" className="vaa-chat-error">{state.error || cancelError}</p>}
        {state.readFailed && <button type="button" className="vaa-chat-open" onClick={() => void session.current?.retryRead(jobs)}>重新读取答复</button>}
        {canCancel && !loading && <button type="button" className="vaa-chat-open" disabled={canceling} onClick={() => void cancel()}>{canceling ? '正在取消…' : '取消本次提问'}</button>}
        {!state.pending && head.solution_status && head.solution_status !== 'ready' && <p role="status">本次答复有未解决项：{head.solution_gaps?.map(gap => gap.description).join('；') || '请打开已保存内容核对。'}</p>}
        {!state.pending && <label className="vaa-chat-permission"><input type="checkbox" checked={authorized} disabled={blocked} onChange={event => setAuthorized(event.target.checked)}/><span>本次允许将视频文字和对话发送给已配置的模型服务</span></label>}
      </div>}/>
  </aside>
}
