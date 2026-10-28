import {useLayoutEffect, useMemo, useRef, useState, type ReactNode, type ComponentProps, type ComponentRef} from 'react'
import {Bubble, Sender} from '@ant-design/x'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {Button} from '@/components/ui/button'
import {UserRound, Bot, Maximize2, Minimize2, Trash2, X} from 'lucide-react'

export type ChatMode = 'half' | 'full'
export type ChatViewMessage = {id: string; role: 'user' | 'assistant'; content: string; footer?: ReactNode}
type Props = {
  title?: string; messages: ChatViewMessage[]; input: string; onInputChange: (value: string) => void
  onSend: (value: string) => void; loading: boolean; disabled?: boolean; canSend?: boolean
  onCancel?: () => void; mode: ChatMode; onModeChange: (mode: ChatMode) => void
  onClose?: () => void; onClear?: () => void; footer?: ReactNode; empty?: ReactNode
  visible?: boolean
}

/** Presentation extracted from BiliNote ChatPanel; no indexing, storage, or model requests. */
export default function ChatPanelView({title = 'AI 问答', messages, input, onInputChange, onSend,
  loading, disabled = false, canSend = true, onCancel, mode, onModeChange, onClose, onClear, footer, empty, visible = true}: Props) {
  const list = useRef<ComponentRef<typeof Bubble.List>>(null)
  const followLatest = useRef(true)
  const [awayFromLatest, setAwayFromLatest] = useState(false)
  const items = useMemo(() => [
    ...messages.map(message => ({key: message.id, role: message.role === 'user' ? 'user' : 'ai',
      content: message.content, footer: message.footer})),
    ...(loading ? [{key: 'loading', role: 'ai', content: '', loading: true}] : []),
  ], [messages, loading])
  const lastItem = items.at(-1)?.key
  useLayoutEffect(() => {
    if (visible && followLatest.current) list.current?.scrollTo({top: 'bottom', behavior: 'auto'})
  }, [lastItem, loading, visible])
  function showLatest() {
    followLatest.current = true
    setAwayFromLatest(false)
    list.current?.scrollTo({top: 'bottom', behavior: 'auto'})
  }
  const roles = useMemo(() => ({
    user: {placement: 'end' as const, avatar: <div className="flex h-7 w-7 items-center justify-center rounded-full bg-blue-500 text-white"><UserRound className="h-4 w-4"/></div>,
      variant: 'filled' as const, styles: {content: {background: '#3b82f6', color: '#fff'}}},
    ai: {placement: 'start' as const, avatar: <div className="flex h-7 w-7 items-center justify-center rounded-full bg-neutral-500 text-white"><Bot className="h-4 w-4"/></div>,
      variant: 'outlined' as const, contentRender: (content: unknown) => <div className="markdown-body prose prose-sm max-w-none prose-p:my-1 prose-li:my-0.5 prose-headings:my-2">
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
          a: ({children, href}: ComponentProps<'a'>) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
          img: ({src, alt}: ComponentProps<'img'>) => typeof src === 'string' && src.startsWith('/api/bridge/assets/')
            ? <img src={src} alt={alt || ''}/> : <span>{alt || '图片'}</span>,
        }}>{String(content).split('(/bridge/assets/').join('(/api/bridge/assets/')}</ReactMarkdown>
      </div>},
  }), [])
  return <div className="vaa-chat-view flex h-full min-h-0 flex-col border-l">
    <div className="flex items-center justify-between border-b px-3 py-2">
      <span className="text-sm font-medium">{title}</span>
      <div className="flex items-center gap-1">
        <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-neutral-400" onClick={() => onModeChange(mode === 'half' ? 'full' : 'half')} aria-label={mode === 'half' ? '展开问答' : '恢复并排阅读'}>
          {mode === 'half' ? <Maximize2 className="h-3.5 w-3.5"/> : <Minimize2 className="h-3.5 w-3.5"/>}
        </Button>
        {onClear && messages.length > 0 && <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-neutral-400" onClick={onClear} aria-label="清空对话"><Trash2 className="h-3.5 w-3.5"/></Button>}
        {onClose && <Button type="button" variant="ghost" size="sm" className="h-7 px-2 text-neutral-400" onClick={onClose} aria-label="收起问答"><X className="h-3.5 w-3.5"/></Button>}
      </div>
    </div>
    <div className="min-h-0 flex-1 overflow-hidden">
      {messages.length === 0 && !loading ? <div className="flex h-full items-center justify-center px-4 text-center text-sm text-neutral-400">{empty || '针对正在阅读的内容提问'}</div>
        : <Bubble.List ref={list} items={items} role={roles} style={{height: '100%'}} onScroll={event => {
          // Installed BiliNote Bubble.List uses a reversed scroll box: bottom is zero.
          const atLatest = Math.abs(event.currentTarget.scrollTop) < 16
          followLatest.current = atLatest
          setAwayFromLatest(!atLatest)
        }}/>}
    </div>
    {awayFromLatest && <div className="flex justify-end px-3 py-1"><Button type="button" variant="ghost" size="sm" onClick={showLatest}>最新回复</Button></div>}
    <div className="border-t px-3 py-2">
      {footer}
      <Sender value={input} onChange={onInputChange} onSubmit={value => {followLatest.current = true; onSend(value)}} loading={loading} disabled={disabled}
        onCancel={onCancel} placeholder="输入问题…" autoSize={{minRows: 2, maxRows: 6}}
        suffix={(_, {components: {SendButton, LoadingButton}}) => loading
          ? <LoadingButton aria-label="取消本次提问" disabled={!onCancel}/>
          : <SendButton aria-label="发送问题" disabled={disabled || !canSend || !input.trim()}/>}/>
    </div>
  </div>
}
