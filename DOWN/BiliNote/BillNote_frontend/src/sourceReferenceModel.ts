export type SourceSegment = {start: number; end: number; text: string}
export type SourcePassage = {from: number; to: number; matches: number[]}

export function validSourceSegment(segment: SourceSegment) {
  return Number.isFinite(segment.start) && Number.isFinite(segment.end)
    && segment.start >= 0 && segment.end >= segment.start
    && typeof segment.text === 'string' && Boolean(segment.text.trim())
}

export function parseSourceTime(value: string): number | null {
  const text = value.trim()
  if (!/^\d+(?::\d{1,2}){0,2}(?:\.\d+)?$/.test(text)) return null
  const parts = text.split(':').map(Number)
  if (!parts.every(Number.isFinite) || parts.slice(1).some(part => part >= 60)) return null
  return parts.reduce((total, part) => total * 60 + part, 0)
}

export function formatSourceTime(value: number) {
  const seconds = Math.floor(value)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor(seconds / 60) % 60
  const rest = String(seconds % 60).padStart(2, '0')
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`
}

export function videoReferenceUrl(source: string, seconds: number): string | null {
  if (!Number.isFinite(seconds) || seconds < 0) return null
  try {
    const original = new URL(source)
    if (original.protocol !== 'https:' || original.username || original.password
      || !['www.bilibili.com', 'bilibili.com'].includes(original.hostname)
      || !/^\/video\/BV[0-9A-Za-z]{10}\/?$/.test(original.pathname)) return null
    const page = original.searchParams.get('p') || '1'
    if (!/^[1-9]\d*$/.test(page)) return null
    const target = new URL(`https://www.bilibili.com${original.pathname}`)
    target.searchParams.set('p', page)
    target.searchParams.set('t', String(Math.floor(seconds)))
    return target.toString()
  } catch {
    return null
  }
}

export function findSourcePassages(segments: readonly SourceSegment[], query: string, seconds: number | null) {
  const needle = query.trim().replace(/\s+/g, ' ').toLocaleLowerCase()
  const hits: number[] = []
  segments.forEach((segment, index) => {
    if (!validSourceSegment(segment)) return
    if (seconds !== null
      ? Number.isFinite(seconds) && seconds >= segment.start && seconds <= segment.end
      : needle && segment.text.replace(/\s+/g, ' ').toLocaleLowerCase().includes(needle)) hits.push(index)
  })
  const passages: SourcePassage[] = []
  for (const index of hits) {
    const from = Math.max(0, index - 1)
    const to = Math.min(segments.length - 1, index + 1)
    const previous = passages.at(-1)
    if (previous && from <= previous.to) {
      previous.to = Math.max(previous.to, to)
      previous.matches.push(index)
    } else {
      passages.push({from, to, matches: [index]})
    }
  }
  return {hitCount: hits.length, passages}
}
