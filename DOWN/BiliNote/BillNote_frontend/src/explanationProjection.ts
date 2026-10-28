import type {BoundExplanation} from './ExplanationView'

export type ExplanationReadingPart =
  | {kind: 'markdown'; content: string}
  | {kind: 'explanation'; value: BoundExplanation}

type Marker = {id: string; closing: boolean; start: number; end: number}

/** Read-only presentation: the stored Markdown remains the copy/edit/export source. */
export function projectExplanationBlocks(markdown: string, values: readonly BoundExplanation[] = []): ExplanationReadingPart[] {
  const original: ExplanationReadingPart[] = [{kind: 'markdown', content: markdown}]
  if (!values.length) return original
  // Independent Markdown fragments cannot safely share reference definitions.
  if (/^ {0,3}\[[^\]\r\n]+\]:[ \t]*/m.test(markdown)) return original

  const bound = new Map<string, BoundExplanation>()
  const duplicateValues = new Set<string>()
  for (const value of values) {
    if (bound.has(value.call_id)) duplicateValues.add(value.call_id)
    bound.set(value.call_id, value)
  }
  const counts = new Map<string, {open: number; close: number}>()
  for (const match of markdown.matchAll(/<!--(\/?)vaa-explanation:([a-z][a-z0-9_]{0,30})-->/g)) {
    const count = counts.get(match[2]) ?? {open: 0, close: 0}
    if (match[1]) count.close++
    else count.open++
    counts.set(match[2], count)
  }

  const markers: Marker[] = []
  let fence: {character: string; length: number} | null = null
  let inlineTicks = 0
  let comment = false
  for (const line of markdown.matchAll(/[^\r\n]*(?:\r\n|\n|\r|$)/g)) {
    if (!line[0]) continue
    const text = line[0].replace(/[\r\n]+$/, '')
    if (fence) {
      const closing = /^ {0,3}(`+|~+)[ \t]*$/.exec(text)
      if (closing && closing[1][0] === fence.character && closing[1].length >= fence.length) fence = null
      continue
    }
    if (/^(?: {4}|\t)/.test(text)) continue
    if (!inlineTicks && !comment) {
      const opening = /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(text)
      if (opening && (opening[1][0] !== '`' || !opening[2].includes('`'))) {
        fence = {character: opening[1][0], length: opening[1].length}
        continue
      }
      const marker = /^<!--(\/?)vaa-explanation:([a-z][a-z0-9_]{0,30})-->[ \t]*$/.exec(text)
      if (marker) {
        markers.push({id: marker[2], closing: Boolean(marker[1]), start: line.index, end: line.index + line[0].length})
        continue
      }
    }
    // Do not promote literal marker examples inside code spans or larger comments.
    for (const token of text.matchAll(/<!--|-->|`+/g)) {
      if (comment) {
        if (token[0] === '-->') comment = false
        continue
      }
      if (token[0][0] === '`') {
        const slashes = /\\*$/.exec(text.slice(0, token.index))?.[0].length ?? 0
        if (slashes % 2) continue
        if (inlineTicks === token[0].length) inlineTicks = 0
        else if (!inlineTicks) inlineTicks = token[0].length
      } else if (!inlineTicks && token[0] === '<!--') comment = true
    }
  }

  const ranges: {start: number; end: number; value: BoundExplanation}[] = []
  let opening: Marker | null = null
  for (const marker of markers) {
    if (!marker.closing) {
      if (opening) return original
      opening = marker
      continue
    }
    if (!opening || opening.id !== marker.id) return original
    const value = bound.get(marker.id)
    const count = counts.get(marker.id)
    if (value && !duplicateValues.has(marker.id) && count?.open === 1 && count.close === 1) {
      const body = markdown.slice(opening.end, marker.start)
      const images = [...body.matchAll(/!\[[^\]\r\n]*\]\(([^)\r\n]*)\)/g)].map(match => match[1])
      const normalize = (url: string) => url.replace(/^\/api\/bridge\//, '/bridge/')
      if (value.panels.length > 0 && images.length === value.panels.length
        && images.every((url, index) => normalize(url) === normalize(value.panels[index].svg_url))) {
        ranges.push({start: opening.start, end: marker.end, value})
      }
    }
    opening = null
  }
  if (opening || !ranges.length) return original

  const parts: ExplanationReadingPart[] = []
  let cursor = 0
  for (const range of ranges) {
    if (range.start > cursor) parts.push({kind: 'markdown', content: markdown.slice(cursor, range.start)})
    parts.push({kind: 'explanation', value: range.value})
    cursor = range.end
  }
  if (cursor < markdown.length) parts.push({kind: 'markdown', content: markdown.slice(cursor)})
  return parts
}
