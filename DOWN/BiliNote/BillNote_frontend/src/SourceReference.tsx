import {useMemo, useState} from 'react'
import {findSourcePassages, formatSourceTime, parseSourceTime, validSourceSegment, videoReferenceUrl, type SourceSegment} from './sourceReferenceModel'

type Props = {segments: readonly SourceSegment[]; sourceUrl: string}

export default function SourceReference({segments, sourceUrl}: Props) {
  const [mode, setMode] = useState<'words' | 'time'>('words')
  const [query, setQuery] = useState('')
  const [visible, setVisible] = useState(6)
  const [showAll, setShowAll] = useState(false)
  const valid = useMemo(() => segments.filter(validSourceSegment), [segments])
  const time = mode === 'time' ? parseSourceTime(query) : null
  const invalidTime = mode === 'time' && Boolean(query.trim()) && time === null
  const found = useMemo(() => findSourcePassages(valid, mode === 'words' ? query : '', time), [valid, mode, query, time])
  const searching = Boolean(query.trim()) && !invalidTime

  function row(segment: SourceSegment, index: number, matched = false) {
    const link = videoReferenceUrl(sourceUrl, segment.start)
    const timeText = `${formatSourceTime(segment.start)}–${formatSourceTime(segment.end)}`
    return <li key={index} className={`vaa-reference-row${matched ? ' is-match' : ''}`}>
      <div className="vaa-reference-time">
        {link ? <a href={link} target="_blank" rel="noopener noreferrer" title="打开这段时间的原视频">{timeText} ↗</a> : <span>{timeText}</span>}
        {matched && <span className="vaa-reference-match">匹配片段</span>}
      </div>
      <p>{segment.text}</p>
    </li>
  }

  return <details className="vaa-reference">
    <summary>查找原文</summary>
    <p className="vaa-hint">需要核对某句话时再查找。这里是识别转写，可能有误；时间链接可打开对应分 P 的原视频。</p>
    {valid.length === 0 ? <p className="vaa-message">这份结果没有可用的转写片段，暂时无法定位原话。</p> : <>
      <div className="vaa-reference-search">
        <label className="vaa-field"><span>查找方式</span><select value={mode} onChange={event => {setMode(event.target.value as 'words' | 'time'); setQuery(''); setVisible(6); setShowAll(false)}}>
          <option value="words">按词句</option><option value="time">按时间</option>
        </select></label>
        <label className="vaa-field"><span>{mode === 'words' ? '转写中的词句' : '视频时间'}</span><input type="search" value={query} onChange={event => {setQuery(event.target.value); setVisible(6); setShowAll(false)}} placeholder={mode === 'words' ? '输入转写中的词句，可查看前后上下文' : '例如 12:30 或秒数 750'}/></label>
      </div>
      {invalidTime && <p role="alert" className="vaa-message is-warning">时间格式应为分:秒、时:分:秒或秒数，例如 12:30、1:02:03、750。</p>}
      {!query.trim() && <p className="vaa-hint">输入要核对的词句或时间。也可以在下方展开完整转写。</p>}
      {searching && <div role="status" className="vaa-hint">{found.hitCount ? `找到 ${found.hitCount} 个匹配片段，以下包含相邻上下文。` : mode === 'words' ? '没有匹配到这些词句。可换用转写中的说法或按时间查找；这不表示视频没有讲到。' : '这个时间没有对应转写片段，可换一个相邻时间；这不表示该时刻没有声音。'}</div>}
      {searching && found.passages.slice(0, visible).map(passage => <ol key={passage.from} className="vaa-reference-passage">
        {valid.slice(passage.from, passage.to + 1).map((segment, offset) => row(segment, passage.from + offset, passage.matches.includes(passage.from + offset)))}
      </ol>)}
      {searching && found.passages.length > visible && <button type="button" className="vaa-secondary" onClick={() => setVisible(value => value + 6)}>显示更多匹配</button>}
      <div className="vaa-reference-all">
        <button type="button" className="vaa-secondary" aria-expanded={showAll} onClick={() => setShowAll(value => !value)}>{showAll ? '收起完整转写' : `展开完整转写（${valid.length} 段）`}</button>
        {showAll && <ol className="vaa-reference-transcript">{valid.map((segment, index) => row(segment, index))}</ol>}
      </div>
    </>}
  </details>
}
