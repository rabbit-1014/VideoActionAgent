import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {test} from 'node:test'
import ts from 'typescript'

const source = readFileSync(new URL('./savedContentLink.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}})
const namespace = {}
new Function('exports', compiled.outputText)(namespace)
const {parseContentTarget, chooseSavedContent} = namespace

const id = '123e4567-e89b-12d3-a456-426614174000'

test('missing content query leaves selection to legacy behavior', () => {
  assert.equal(parseContentTarget(''), null)
  assert.equal(parseContentTarget('?other=value'), null)
})

test('a single UUID is canonicalized and takes priority over stored selection', () => {
  assert.equal(parseContentTarget(`?content=${id.toUpperCase()}`), id)
  assert.equal(chooseSavedContent(id, '', 'older', ['older', id]), id)
})

test('invalid, empty, or repeated targets are explicit and do not fall back', () => {
  for (const search of ['?content=', '?content=latest', `?content=${id}&content=${id}`, `?content=${id}&content=`]) {
    const target = parseContentTarget(search)
    assert.equal(target, '')
    assert.equal(chooseSavedContent(target, '', 'older', ['older', id]), '')
  }
})

test('an unknown target stays selected instead of choosing another available result', () => {
  assert.equal(chooseSavedContent(id, '', 'older', ['older']), id)
})

test('a completed revision overrides a link target and an explicit hand selection can leave it', () => {
  assert.equal(chooseSavedContent(id, 'new-version', 'older', ['older', 'new-version']), 'new-version')
  assert.equal(chooseSavedContent(null, '', 'hand-picked', ['older', 'hand-picked']), 'hand-picked')
})

test('no-query mode retains the previous available/current/most-recent selection rules', () => {
  assert.equal(chooseSavedContent(null, '', 'current', ['current', 'recent']), 'current')
  assert.equal(chooseSavedContent(null, '', '', ['older', 'recent']), 'recent')
  assert.equal(chooseSavedContent(null, '', 'missing', ['recent']), '')
})
