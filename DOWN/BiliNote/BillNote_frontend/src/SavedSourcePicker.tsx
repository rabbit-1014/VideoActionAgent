import {useState} from 'react'
import {filterSavedSources, groupSavedSources, type SavedVideoSource} from './savedSources'

function duration(source: SavedVideoSource) {
  if (typeof source.duration_seconds !== 'number' || !Number.isFinite(source.duration_seconds) || source.duration_seconds <= 0) return ''
  const total = Math.round(source.duration_seconds), hours = Math.floor(total / 3600)
  const minutes = Math.floor(total % 3600 / 60), seconds = String(total % 60).padStart(2, '0')
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`
}

function readTime(source: SavedVideoSource) {
  const time = source.observed_at ? new Date(source.observed_at) : null
  return time && Number.isFinite(time.getTime()) ? time.toLocaleString('zh-CN', {hour12: false}) : '时间未记录'
}

export default function SavedSourcePicker({sources, value, onChange}: {
  sources: SavedVideoSource[]; value: string; onChange: (sourceId: string) => void;
}) {
  const [query, setQuery] = useState('')
  const groups = groupSavedSources(sources), matches = filterSavedSources(groups, query)
  const chosen = sources.find(source => source.source_id === value)
  const chosenGroup = groups.find(group => group.records.some(source => source.source_id === value))
  return <div className="vaa-source-picker">
    <label className="vaa-field"><span>查找已读取的视频</span>
      <input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索标题、BV 编号或分P，例如 P42"/>
    </label>
    <p className="vaa-hint">{groups.length} 个视频，选择后使用已保存的内容，不重新读取视频。</p>
    <div className="vaa-source-options" aria-label="可选择的视频">
      {matches.map(group => <button type="button" key={group.key} data-video-key={group.key}
        className={`vaa-source-option${chosenGroup?.key === group.key ? ' is-selected' : ''}`}
        aria-pressed={chosenGroup?.key === group.key}
        onClick={() => onChange(group.records[0].source_id)}>
        <strong>{group.title}</strong>
        <span>第 {group.page} P{duration(group.records[0]) ? ` · ${duration(group.records[0])}` : ''} · {group.bvid}</span>
      </button>)}
    </div>
    {matches.length === 0 && <p className="vaa-hint" role="status">{sources.length ? '没有找到匹配的视频，请换个关键词。' : '还没有已读取的视频，请先使用新视频链接。'}</p>}
    {chosen && chosenGroup && <section className="vaa-source-selected" aria-label="已选择的视频">
      <strong>已选择：{chosen.title}</strong>
      <p>第 {chosen.page} P{duration(chosen) ? ` · ${duration(chosen)}` : ''} · {chosen.bvid}</p>
      <p className="vaa-hint">来源读取时间：{readTime(chosen)} · {chosen.saved_visual_available ? '有已保存画面' : '声音文字'}
        {' · '}<a href={`https://www.bilibili.com/video/${encodeURIComponent(chosen.bvid)}/?p=${chosen.page}`} target="_blank" rel="noreferrer">打开原视频核对</a></p>
      {chosenGroup.records.length > 1 && <details className="vaa-source-records"><summary>读取记录（{chosenGroup.records.length} 次）</summary>
        <p className="vaa-hint">列表默认选择最近读取的记录；从已有成果提问时保留该成果原来的来源。</p>
        <label className="vaa-field"><span>本次使用的读取记录</span><select value={value} onChange={event => onChange(event.target.value)}>
          {chosenGroup.records.map((source, index) => <option key={source.source_id} value={source.source_id}>
            记录 {index + 1} · {readTime(source)} · {source.segments} 段文字 · {source.saved_visual_available ? '有画面' : '声音文字'}
          </option>)}
        </select></label>
      </details>}
    </section>}
  </div>
}
