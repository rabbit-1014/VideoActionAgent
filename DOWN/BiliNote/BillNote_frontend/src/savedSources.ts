export type SavedVideoSource = {
  source_id: string; bvid: string; page: number; cid: number; title: string;
  segments: number; acquisition: string; saved_visual_available: boolean;
  observed_at?: string | null; duration_seconds?: number | null;
}

export type SavedSourceGroup = {
  key: string; bvid: string; page: number; title: string; records: SavedVideoSource[];
}

function observedTime(source: SavedVideoSource) {
  const time = source.observed_at ? Date.parse(source.observed_at) : NaN;
  return Number.isFinite(time) ? time : 0;
}

export function groupSavedSources(sources: SavedVideoSource[]): SavedSourceGroup[] {
  const groups = new Map<string, SavedSourceGroup>();
  for (const source of sources) {
    const key = `${source.bvid}:p${source.page}`;
    let group = groups.get(key);
    if (!group) {
      group = {key, bvid: source.bvid, page: source.page, title: source.title, records: []};
      groups.set(key, group);
    }
    // Group only the menu. Every original material/visual scope keeps its ID.
    group.records.push(source);
  }
  for (const group of groups.values()) {
    group.records.sort((a, b) => observedTime(b) - observedTime(a));
    group.title = group.records[0].title;
  }
  return [...groups.values()].sort((a, b) => observedTime(b.records[0]) - observedTime(a.records[0]));
}

export function filterSavedSources(groups: SavedSourceGroup[], query: string) {
  const terms = query.normalize('NFKC').trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return groups.filter(group => {
    const searchable = `${group.records.map(r => r.title).join(' ')} ${group.bvid} p${group.page} 第${group.page}分P`.normalize('NFKC').toLocaleLowerCase();
    return terms.every(term => searchable.includes(term));
  });
}
