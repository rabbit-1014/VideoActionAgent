const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function parseContentTarget(search: string): string | null {
  const params = new URLSearchParams(search)
  const values = params.getAll('content')
  if (values.length === 0) return null
  if (values.length !== 1 || !UUID_PATTERN.test(values[0])) return ''
  return values[0].toLowerCase()
}

export function chooseSavedContent(
  linkTarget: string | null,
  revisionJobId: string,
  selectedAtResponse: string,
  availableJobIds: readonly string[],
): string {
  if (revisionJobId && availableJobIds.includes(revisionJobId)) return revisionJobId
  if (linkTarget !== null) return linkTarget
  if (availableJobIds.includes(selectedAtResponse)) return selectedAtResponse
  return selectedAtResponse ? '' : availableJobIds.at(-1) || ''
}

export function replaceContentTarget(jobId?: string) {
  const params = new URLSearchParams(location.search)
  params.delete('content')
  if (jobId) params.set('content', jobId)
  const search = params.toString()
  history.replaceState(null, '', `${location.pathname}${search ? `?${search}` : ''}`)
}
