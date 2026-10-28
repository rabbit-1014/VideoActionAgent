'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const element = (tag, text = '') => { const node = document.createElement(tag); node.textContent = text; return node; };
  let binding = null, sequence = 0;

  function clear() {
    sequence++; binding = null;
    $('notes-reference-card').hidden = true; $('notes-reference-reader').hidden = true;
    $('notes-reference-content').replaceChildren(); $('notes-reference-error').textContent = '';
  }

  // Only the pinned document's headings, paragraphs, list, table and HTTPS source link.
  // All words use textContent; Markdown can never create HTML, scripts or local file links.
  function inline(node, text) {
    const tokens = /\*\*([^*]+)\*\*|\[([^\]]+)\]\((https:\/\/[^\s)]+)\)/g;
    let start = 0, match;
    while ((match = tokens.exec(text))) {
      node.append(document.createTextNode(text.slice(start, match.index)));
      if (match[1]) node.append(element('strong', match[1]));
      else if (match[3] === binding.source_url) {
        const link = element('a', match[2]); link.href = match[3];
        link.target = '_blank'; link.rel = 'noopener noreferrer'; node.append(link);
      } else node.append(document.createTextNode(match[2]));
      start = tokens.lastIndex;
    }
    node.append(document.createTextNode(text.slice(start)));
  }

  function render(markdown) {
    const content = $('notes-reference-content'); content.replaceChildren();
    const lines = markdown.split(/\r?\n/); let table = null, list = null, paragraph = [];
    function flush() {
      if (paragraph.length) { const node = element('p'); inline(node, paragraph.join(' ')); content.append(node); paragraph = []; }
    }
    for (const line of lines) {
      if (!line.trim()) { flush(); table = null; list = null; continue; }
      if (line.startsWith('# ') || line.startsWith('原稿：')) { flush(); continue; }
      if (line.startsWith('## ')) { flush(); content.append(element('h3', line.slice(3))); continue; }
      if (line.startsWith('|')) {
        flush();
        if (/^\|[-\s|]+\|$/.test(line)) continue;
        const header = !table;
        if (!table) {
          const wrap = element('div'); wrap.className = 'reference-table';
          table = element('table'); wrap.append(table); content.append(wrap);
        }
        const row = element('tr');
        for (const cell of line.slice(1, -1).split('|')) {
          const node = element(header ? 'th' : 'td'); inline(node, cell.trim()); row.append(node);
        }
        table.append(row); continue;
      }
      const item = /^\d+\.\s+(.+)$/.exec(line);
      if (item) {
        flush(); if (!list) { list = element('ol'); content.append(list); }
        const node = element('li'); inline(node, item[1]); list.append(node); continue;
      }
      paragraph.push(line);
    }
    flush();
  }

  async function read(suffix = '') {
    const ticket = sequence, selected = binding;
    if (!selected || selected.state !== 'available') throw new Error('这份参考当前不可用。');
    const response = await fetch(`/app/notes/${selected.candidate_id}/reference${suffix}`, {cache: 'no-store'});
    if (ticket !== sequence) return null;
    if (!response.ok) throw new Error(response.status === 401 ? '会话已失效，请重新连接。' : '关联版本或参考文件已不可用，请重新打开原稿核对。');
    if (suffix) return {blob: await response.blob(), ticket};
    const value = await response.json(), actual = value.reference;
    if (ticket !== sequence) return null;
    if (actual?.state !== 'available' || actual.candidate_id !== selected.candidate_id ||
        actual.record_fingerprint !== selected.record_fingerprint ||
        actual.payload_sha256 !== selected.payload_sha256 || typeof value.markdown !== 'string')
      throw new Error('参考关联未确认，请重新打开原稿核对。');
    return value;
  }

  window.addEventListener('vaa-candidate-cleared', clear);
  window.addEventListener('vaa-candidate-opened', event => {
    clear(); const detail = event.detail || {}, reference = detail.independent_reference;
    if (!reference || reference.candidate_id !== detail.candidate_id ||
        (reference.state === 'available' && reference.record_fingerprint !== detail.record_fingerprint)) return;
    binding = reference; $('notes-reference-card').hidden = false;
    $('notes-reference-label').textContent = reference.title;
    $('notes-reference-state').textContent = reference.state === 'available' ?
      `独立外部参考，关联第 ${reference.version} 版。政策文档更新于 ${reference.source_document_updated}，不是视频原话或现行软件运行证明。` :
      '这份关联参考当前不可用。请核对原稿版本和参考文件；不会改用其他稿件或参考。';
    $('notes-reference-open').disabled = reference.state !== 'available';
  });
  $('notes-reference-open').addEventListener('click', async () => {
    const ticket = sequence; $('notes-reference-error').textContent = '';
    $('notes-reference-open').disabled = true;
    try {
      const value = await read(); if (!value || ticket !== sequence) return;
      $('notes-reference-title').textContent = binding.title;
      $('notes-reference-meta').textContent = `独立外部参考 · 关联操作笔记第 ${binding.version} 版 · 文档更新日期 ${binding.source_document_updated}`;
      render(value.markdown); $('notes-reference-reader').hidden = false;
      $('notes-reference-reader').scrollIntoView({block: 'start'}); $('notes-reference-back').focus({preventScroll: true});
    } catch (error) {
      if (ticket === sequence) { $('notes-reference-reader').hidden = true; $('notes-reference-content').replaceChildren(); $('notes-reference-error').textContent = error.message; }
    } finally { if (ticket === sequence) $('notes-reference-open').disabled = false; }
  });
  $('notes-reference-back').addEventListener('click', () => {
    $('notes-reference-reader').hidden = true;
    $('notes-result').scrollIntoView({block: 'start'}); $('notes-reference-open').focus({preventScroll: true});
  });
  $('notes-reference-download').addEventListener('click', async () => {
    const ticket = sequence;
    try {
      const value = await read('/download'); if (!value || ticket !== sequence) return;
      const url = URL.createObjectURL(value.blob), link = element('a');
      link.href = url; link.download = '剪映字幕方法-独立参考.md'; document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { if (ticket === sequence) $('notes-reference-error').textContent = error.message; }
  });
})();
