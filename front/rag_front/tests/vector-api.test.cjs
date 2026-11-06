const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')
const ts = require('typescript')

// Execute real API modules with mocked transport: no web server or network is used.
function load(relative, imports) {
  const source = fs.readFileSync(path.join(__dirname, '..', relative), 'utf8')
  const result = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const exports = {}
  const sandbox = { exports, FormData, require: (name) => {
    if (!(name in imports)) throw Error(`Unmocked dependency: ${name}`)
    return imports[name]
  } }
  vm.runInNewContext(result.replaceAll('import.meta.env.VITE_API_BASE_URL', 'undefined'), sandbox)
  return exports
}

function transport() {
  const calls = []
  const http = Object.fromEntries(['get', 'post', 'patch', 'delete'].map(method => [method, async (...args) => {
    calls.push({ method, args })
    return { data: {} }
  }]))
  return { http, calls }
}

test('four vector write operations wait 180 seconds and preserve payloads', async () => {
  const { http, calls } = transport()
  const dependency = { http, VECTOR_WRITE_TIMEOUT_MS: 180000 }
  const documents = load('src/api/modules/documents.ts', { '@/api/http': dependency })
  const chunks = load('src/api/modules/chunks.ts', { '@/api/http': dependency })
  await documents.ingestTextDocument({ filename: 'a.txt', content: 'body', knowledge_base: 'default' })
  await documents.uploadDocument({ file: new Blob(['body']), knowledge_base: 'default' })
  await documents.rebuildDocumentIndex('d1', 'structured')
  await chunks.updateChunk('c1', { metadata_json: { section_title: 'Title' } })
  for (const { args } of calls) assert.equal(args[2].timeout, 180000)
  assert.equal(calls[0].args[1].content, 'body')
  assert.equal(calls[1].args[1].get('knowledge_base'), 'default')
  assert.equal(calls[2].args[1].preferred_splitter, 'structured')
  assert.equal(calls[3].args[1].metadata_json.section_title, 'Title')
})

test('read and delete operations keep the default timeout', async () => {
  const { http, calls } = transport()
  const dependency = { http, VECTOR_WRITE_TIMEOUT_MS: 180000 }
  const documents = load('src/api/modules/documents.ts', { '@/api/http': dependency })
  const chunks = load('src/api/modules/chunks.ts', { '@/api/http': dependency })
  await documents.fetchDocuments()
  await documents.fetchDocumentChunks('d1')
  await documents.deleteDocument('d1')
  await chunks.fetchChunks({ limit: 10 })
  await chunks.deleteChunk('c1')
  for (const { args } of calls) assert.equal(args[2]?.timeout, undefined)
})

test('browser timeout says processing may continue, and server detail remains visible', () => {
  const messages = []
  const axios = {
    isAxiosError: value => value?.isAxiosError === true,
    create: config => {
      assert.equal(config.timeout, 30000)
      return { interceptors: { response: { use() {} } } }
    },
  }
  const module = load('src/api/http.ts', {
    axios: { __esModule: true, default: axios }, 'element-plus': { ElMessage: { error: message => messages.push(message) } },
  })
  for (const code of ['ECONNABORTED', 'ETIMEDOUT']) {
    const message = module.notifyError({ isAxiosError: true, code, config: { timeout: 180000 } })
    assert.match(message, /后端可能仍在处理/)
    assert.match(message, /避免重复提交/)
    assert.doesNotMatch(message, /已取消/)
  }
  assert.equal(module.notifyError({ isAxiosError: true, response: { data: { detail: '向量数量异常' } } }), '向量数量异常')
  assert.equal(messages.length, 3)
})
