import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('./explanationProjection.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020}})
const namespace = {}
new Function('exports', compiled.outputText)(namespace)
const {projectExplanationBlocks} = namespace

function explanation(call_id = 'example', panel_id = 'first') {
  return {call_id, title: '同一例子', purpose: '内部工具理由', subject: '对象', panels: [{
    id: panel_id, title: '当前状态', explanation: '局部说明', provenance: 'illustrative',
    svg_url: `/api/bridge/assets/12345678-1234-1234-1234-123456789abc/goal_explanation_1_${panel_id}.svg`,
  }]}
}

function block(value) {
  return [`<!--vaa-explanation:${value.call_id}-->`, '### 同一例子', ...value.panels.flatMap(panel => [
    '', `#### ${panel.title}`, '', `![解释性示例](${panel.svg_url})`, '', panel.explanation,
  ]), `<!--/vaa-explanation:${value.call_id}-->`].join('\n')
}

const value = explanation()
const fallback = markdown => [{kind: 'markdown', content: markdown}]

test('ordinary Markdown is preserved exactly', () => {
  const markdown = '# 正文\n\n正文与 [引用][x]。\n\n[x]: /local\n'
  assert.deepEqual(projectExplanationBlocks(markdown, []), fallback(markdown))
})

test('one matching block is replaced in place without changing stored text or metadata', () => {
  const markdown = `前文。\n\n${block(value)}\n\n后文。\n`
  const snapshot = JSON.stringify(value)
  assert.deepEqual(projectExplanationBlocks(markdown, [value]), [
    {kind: 'markdown', content: '前文。\n\n'}, {kind: 'explanation', value}, {kind: 'markdown', content: '\n后文。\n'},
  ])
  assert.equal(markdown, `前文。\n\n${block(value)}\n\n后文。\n`)
  assert.equal(JSON.stringify(value), snapshot)
})

test('bound blocks follow document order, not metadata order', () => {
  const other = explanation('second', 'second')
  const parts = projectExplanationBlocks(`${block(value)}\n\n中间正文\n\n${block(other)}`, [other, value])
  assert.deepEqual(parts.filter(part => part.kind === 'explanation').map(part => part.value.call_id), ['example', 'second'])
  assert.equal(parts.filter(part => part.kind === 'markdown').map(part => part.content).join(''), '\n中间正文\n\n')
})

test('fenced code, including longer backtick fences and tilde fences, stays literal', () => {
  for (const fence of ['```markdown', '~~~~', '````markdown']) {
    const closer = fence.startsWith('~') ? '~~~~' : fence.startsWith('````') ? '````' : '```'
    const markdown = `${fence}\n${block(value)}\n${closer}\n`
    assert.deepEqual(projectExplanationBlocks(markdown, [value]), fallback(markdown))
  }
})

test('indented code, inline code and enclosing HTML comments stay literal', () => {
  for (const markdown of [
    block(value).split('\n').map(line => `    ${line}`).join('\n'),
    `\`示例\n${block(value)}\n\``,
    `<!-- 这是被注释的示例\n${block(value)}\n-->`,
  ]) assert.deepEqual(projectExplanationBlocks(markdown, [value]), fallback(markdown))
})

test('duplicate markers or duplicate bound identities never remove fallback content', () => {
  const markdown = `${block(value)}\n\n${block(value)}`
  assert.deepEqual(projectExplanationBlocks(markdown, [value]), fallback(markdown))
  assert.deepEqual(projectExplanationBlocks(block(value), [value, value]), fallback(block(value)))
})

test('missing, crossed, nested or inline markers preserve original text', () => {
  const other = explanation('second', 'second')
  for (const markdown of [
    block(value).replace('<!--/vaa-explanation:example-->', ''),
    block(value).replace('<!--/vaa-explanation:example-->', '<!--/vaa-explanation:second-->'),
    `<!--vaa-explanation:example-->\n${block(other)}\n<!--/vaa-explanation:example-->`,
    `正文 ${block(value)}`,
  ]) assert.deepEqual(projectExplanationBlocks(markdown, [value, other]), fallback(markdown))
})

test('a block without current bound metadata is kept', () => {
  assert.deepEqual(projectExplanationBlocks(block(value), [explanation('other')]), fallback(block(value)))
})

test('a stale, missing or extra panel asset preserves fallback', () => {
  for (const markdown of [
    block(value).replace('123456789abc', '123456789def'),
    block(value).replace(/!\[[^\n]+\n/, ''),
    block(value).replace('<!--/vaa', '![多余图片](/api/bridge/assets/extra.svg)\n<!--/vaa'),
  ]) assert.deepEqual(projectExplanationBlocks(markdown, [value]), fallback(markdown))
})

test('portable asset routes and CRLF are supported without rewriting surrounding text', () => {
  const markdown = `前文\r\n\r\n${block(value).replaceAll('/api/bridge/', '/bridge/').replaceAll('\n', '\r\n')}\r\n\r\n后文`
  assert.deepEqual(projectExplanationBlocks(markdown, [value]), [
    {kind: 'markdown', content: '前文\r\n\r\n'}, {kind: 'explanation', value}, {kind: 'markdown', content: '\r\n后文'},
  ])
})
