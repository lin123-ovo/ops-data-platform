const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')
const ts = require('typescript')
function load(file, http) {
  const source = fs.readFileSync(path.join(__dirname, '../src/api/modules', file), 'utf8')
  const exports = {}
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText,
    { exports, require(name) { if (name !== '@/api/http') throw Error(name); return { http } } })
  return exports
}
test('retrieval forwards graph mode and scope; old calls retain hybrid default', async () => {
  const calls = []
  const api = load('retrieval.ts', { post: async (...args) => { calls.push(args); return { data: { items: [], graph_status: 'degraded' } } } })
  const result = await api.searchRetrieval('q', 5, 'graph_hybrid', '企业文档')
  assert.equal(calls[0][1].knowledge_base, '企业文档')
  assert.equal(calls[0][1].retrieval_mode, 'graph_hybrid')
  assert.equal(result.graph_status, 'degraded')
  await api.searchRetrieval('q', 5)
  assert.equal(calls[1][1].retrieval_mode, 'hybrid')
})
test('graph build, paginated tasks and entity filters preserve their payloads', async () => {
  const calls = []
  const http = Object.fromEntries(['get', 'post'].map(method => [method, async (...args) => { calls.push(args); return { data: {} } }]))
  const api = load('graph.ts', http)
  await api.buildGraph(['d1', 'd2'])
  await api.fetchGraphTasks({ knowledge_base: 'kb', offset: 50 })
  await api.fetchSubgraph({ knowledge_base: 'kb', document_id: 'd1', entity: '组件' })
  assert.equal(calls[0][0], '/graph/build')
  assert.equal(calls[0][1].document_ids.join(','), 'd1,d2')
  assert.equal(calls[1][1].params.offset, 50)
  assert.equal(calls[2][1].params.entity, '组件')
})
