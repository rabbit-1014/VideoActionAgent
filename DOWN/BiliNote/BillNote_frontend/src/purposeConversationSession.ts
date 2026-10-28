/** Controlled conversation over the existing saved-output API, without an AI client. */
export type ConversationTurn = {job_id: string; user_request: string; markdown: string}
export type ConversationAnswer = {
  job_id: string; source_id?: string; state: string; user_request: string
  result?: {markdown: string}
  revision?: {record_fingerprint: string}
  followup?: {parent_job_id: string; expected_parent_fingerprint: string}
  conversation_turns?: ConversationTurn[]
  solution_status?: string
  solution_gaps?: {description: string}[]
}
export type ConversationAdmission = {status: 'accepted' | 'rejected' | 'unknown'; message?: string}
type PendingQuestion = {
  id: string; question: string; parent: ConversationAnswer
  phase: 'submitting' | 'waiting' | 'unknown' | 'reading'
}
export type ConversationState = {
  head: ConversationAnswer; pending: PendingQuestion | null; error: string; readFailed: boolean
}
type Ports = {
  submit: (id: string, question: string, parent: ConversationAnswer) => Promise<ConversationAdmission>
  read: (id: string) => Promise<ConversationAnswer>
}
type JobMetadata = {job_id: string; state: string; error_code?: string | null}

export class ConversationSession {
  state: ConversationState
  private disposed = false
  private readAttempted = false

  constructor(basis: ConversationAnswer, private ports: Ports, private changed: (state: ConversationState) => void) {
    this.state = {head: basis, pending: null, error: '', readFailed: false}
  }

  private publish(patch: Partial<ConversationState>) {
    if (this.disposed) return
    this.state = {...this.state, ...patch}
    this.changed(this.state)
  }
  private pendingQuestion(): PendingQuestion | null { return this.state.pending }

  async send(id: string, value: string, authorized: boolean) {
    const question = value.trim()
    const parent = this.state.head
    if (this.disposed || this.state.pending || !authorized || !question || !parent.source_id ||
        parent.state !== 'available' || !parent.result?.markdown || !parent.revision?.record_fingerprint) return false
    const pending: PendingQuestion = {id, question, parent, phase: 'submitting'}
    this.readAttempted = false
    // Synchronous before I/O: a second click cannot admit another question.
    this.publish({pending, error: '', readFailed: false})
    let admission: ConversationAdmission
    try { admission = await this.ports.submit(id, question, parent) }
    catch { admission = {status: 'unknown', message: '提交结果尚未确认，请等待处理记录。'} }
    if (this.disposed || this.pendingQuestion() !== pending) return true
    if (admission.status === 'rejected') {
      this.publish({pending: null, error: admission.message || '本次请求未创建，请核对后提交。'})
    } else if (pending.phase === 'submitting') {
      this.publish({pending: {...pending, phase: admission.status === 'accepted' ? 'waiting' : 'unknown'},
        error: admission.status === 'unknown' ? admission.message || '提交结果尚未确认，请等待处理记录。' : ''})
    }
    return true
  }

  async observe(jobs: JobMetadata[]) {
    const pending = this.state.pending
    if (this.disposed || !pending) return
    const metadata = jobs.find(job => job.job_id === pending.id)
    if (!metadata) return // Unknown delivery is observed, never replayed.
    if (['canceled', 'failed', 'interrupted'].includes(metadata.state)) {
      this.publish({pending: null, readFailed: false, error: metadata.state === 'canceled'
        ? '本次提问已取消，之前的内容保留。'
        : '本次提问未能得到可用答复，请查看处理记录；之前的内容保留。'})
      return
    }
    if (metadata.state !== 'available' || this.readAttempted) return
    this.readAttempted = true
    const reading = {...pending, phase: 'reading' as const}
    this.publish({pending: reading, readFailed: false, error: ''})
    try {
      const answer = await this.ports.read(pending.id)
      if (this.disposed || this.state.pending !== reading) return
      const parent = pending.parent
      const expected = [...(parent.conversation_turns || []), {
        job_id: parent.job_id, user_request: parent.user_request, markdown: parent.result!.markdown,
      }]
      const historyMatches = Array.isArray(answer.conversation_turns) && answer.conversation_turns.length === expected.length &&
        expected.every((turn, index) => {
          const actual = answer.conversation_turns![index]
          return actual.job_id === turn.job_id && actual.user_request === turn.user_request && actual.markdown === turn.markdown
        })
      if (answer.job_id !== pending.id || answer.state !== 'available' || answer.source_id !== parent.source_id ||
          answer.user_request !== pending.question || !answer.result?.markdown || !answer.revision?.record_fingerprint ||
          answer.followup?.parent_job_id !== parent.job_id ||
          answer.followup.expected_parent_fingerprint !== parent.revision?.record_fingerprint ||
          !historyMatches) {
        throw new Error('答复与所接续的内容不匹配，暂不显示。')
      }
      this.publish({head: answer, pending: null, error: '', readFailed: false})
    } catch (cause) {
      if (this.disposed || this.state.pending !== reading) return
      this.publish({readFailed: true, error: cause instanceof Error ? cause.message : '答复暂时无法读取。'})
    }
  }

  async retryRead(jobs: JobMetadata[]) {
    if (this.disposed || !this.state.readFailed) return
    this.readAttempted = false
    await this.observe(jobs)
  }

  dispose() { this.disposed = true }
}
