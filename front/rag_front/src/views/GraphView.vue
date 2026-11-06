<script setup lang="ts">
import { t } from '@/utils/locale'
import { graphStatusLabel } from '@/utils/graph'
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { fetchGraphTasks, fetchSubgraph, buildGraph } from '@/api/modules/graph'
import { notifyError } from '@/api/http'
import type { GraphAssertion, GraphJob, GraphSubgraph } from '@/types/api'
const route = useRoute()
const form = reactive({ knowledge_base: String(route.query.knowledgeBase || 'default'),
  document_id: String(route.query.documentId || ''), entity: String(route.query.entity || '') })
const graph = ref<GraphSubgraph>({ nodes: [], edges: [] })
const jobs = ref<GraphJob[]>([])
const selected = ref<GraphAssertion | null>(null)
const loading = ref(false)
const offset = ref(0)
const enabled = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
const positions = computed(() => new Map(graph.value.nodes.map((n, i) => {
  const angle = 2 * Math.PI * i / Math.max(graph.value.nodes.length, 1)
  return [n.id, { x: 450 + 320 * Math.cos(angle), y: 250 + 195 * Math.sin(angle) }]
})))
const drawableEdges = computed(() => graph.value.edges.filter(e => positions.value.has(e.subject_id || '') && positions.value.has(e.object_id || '')))
async function refreshTasks() {
  try {
    const data = await fetchGraphTasks({ knowledge_base: form.knowledge_base, document_id: form.document_id || undefined, offset: offset.value })
    jobs.value = data.items
    enabled.value = data.enabled
  } catch (error) { notifyError(error, t('任务状态加载失败', 'Could not load build jobs')) }
}
async function load() {
  if (!form.knowledge_base.trim()) return
  loading.value = true
  selected.value = null
  try {
    graph.value = await fetchSubgraph({ knowledge_base: form.knowledge_base, document_id: form.document_id || undefined, entity: form.entity || undefined })
    await refreshTasks()
  } catch (error) { notifyError(error, t('图谱加载失败', 'Could not load the graph')) }
  finally { loading.value = false }
}
async function retry(documentId: string) {
  try { await buildGraph([documentId]); await refreshTasks() }
  catch (error) { notifyError(error, t('建图提交失败', 'Could not submit the graph build')) }
}
async function page(delta: number) { offset.value = Math.max(0, offset.value + delta); await refreshTasks() }
onMounted(async () => { await load(); timer = setInterval(() => { if (jobs.value.some(j => ['pending', 'processing'].includes(j.status))) void refreshTasks() }, 5000) })
onUnmounted(() => clearInterval(timer))
</script>
<template>
  <div class="page-grid">
    <section class="panel span-12 toolbar-card">
      <div><div class="panel-kicker">Knowledge Graph</div><h2>{{ t('知识图谱与构建任务', 'Knowledge graph and build jobs') }}</h2><p>{{ t('按实体关系查看知识，并追溯每条关系的原文依据。', 'Explore relationships and trace each one to its source.') }}</p></div>
      <div class="toolbar-actions wrap">
        <el-input v-model="form.knowledge_base" :placeholder="t('知识库', 'Knowledge base')" :aria-label="t('知识库', 'Knowledge base')" />
        <el-input v-model="form.document_id" :placeholder="t('文档 ID（可选）', 'Document ID (optional)')" :aria-label="t('文档 ID', 'Document ID')" />
        <el-input v-model="form.entity" :placeholder="t('实体名称或别名（可选）', 'Entity name or alias (optional)')" @keyup.enter="load" :aria-label="t('实体名称', 'Entity name')" />
        <el-button type="primary" :loading="loading" @click="load">{{ t('查询图谱', 'Load graph') }}</el-button>
      </div>
    </section>
    <section class="panel span-12 section-card" v-loading="loading">
      <el-alert v-if="!enabled" :title="t('图谱功能未启用，请完成服务配置后使用', 'Configure the graph service to enable this feature.')" type="info" :closable="false" />
      <el-tag>{{ graph.nodes.length }} {{ t('个实体', 'entities') }} · {{ graph.edges.length }} {{ t('条有据关系', 'supported relationships') }}</el-tag>
      <svg v-if="graph.nodes.length" viewBox="0 0 900 500" role="img" :aria-label="t('知识关系子图', 'Knowledge graph')" style="width:100%;max-height:500px">
        <g v-for="edge in drawableEdges" :key="edge.id" @click="selected = edge" style="cursor:pointer">
          <line :x1="positions.get(edge.subject_id!)!.x" :y1="positions.get(edge.subject_id!)!.y" :x2="positions.get(edge.object_id!)!.x" :y2="positions.get(edge.object_id!)!.y" stroke="#7d96b7" stroke-width="3" />
          <title>{{ edge.predicate }}：{{ edge.evidence }}</title>
        </g>
        <g v-for="node in graph.nodes" :key="node.id" :transform="`translate(${positions.get(node.id)!.x},${positions.get(node.id)!.y})`">
          <circle r="9" fill="#4b83e8" /><text y="-16" text-anchor="middle" fill="currentColor" font-size="13">{{ node.name }}</text>
        </g>
      </svg>
      <el-empty v-else :description="t('暂无有效关系，可先在文档管理中构建图谱', 'No relationships yet. Build a graph from the Documents page.')" />
      <el-table :data="graph.edges" @row-click="(row: GraphAssertion) => selected = row" max-height="320">
        <el-table-column :label="t('主体', 'Subject')"><template #default="{ row }">{{ graph.nodes.find(n => n.id === row.subject_id)?.name }}</template></el-table-column>
        <el-table-column prop="predicate" :label="t('关系', 'Relationship')" />
        <el-table-column :label="t('客体', 'Object')"><template #default="{ row }">{{ graph.nodes.find(n => n.id === row.object_id)?.name }}</template></el-table-column>
        <el-table-column prop="evidence" :label="t('原文证据', 'Source evidence')" show-overflow-tooltip />
      </el-table>
      <el-card v-if="selected" style="margin-top:16px">
        <p style="white-space:pre-wrap">{{ selected.evidence }}</p>
        <router-link :to="{ name: 'chunks', query: { documentId: selected.document_id, chunkId: selected.chunk_id } }">{{ t('定位来源 Chunk', 'Open source chunk') }}</router-link>
      </el-card>
    </section>
    <section class="panel span-12 section-card">
      <h3>{{ t('构建任务', 'Build jobs') }}</h3>
      <el-button @click="refreshTasks">{{ t('刷新进度', 'Refresh progress') }}</el-button>
      <el-table :data="jobs" max-height="360">
        <el-table-column prop="document_id" :label="t('文档', 'Document')" min-width="220" />
        <el-table-column :label="t('状态', 'Status')"><template #default="{ row }">{{ graphStatusLabel(row.status) }}</template></el-table-column>
        <el-table-column :label="t('进度', 'Progress')"><template #default="{ row }">{{ row.progress }} / {{ row.total }}</template></el-table-column>
        <el-table-column prop="attempts" :label="t('尝试次数', 'Attempts')" /><el-table-column prop="schema_version" :label="t('抽取规则版本', 'Extraction rules version')" min-width="150" />
        <el-table-column prop="error" :label="t('失败原因', 'Error')" min-width="180" />
        <el-table-column :label="t('操作', 'Actions')"><template #default="{ row }"><el-button v-if="enabled && ['failed','expired'].includes(row.status)" @click="retry(row.document_id)">{{ t('重试', 'Retry') }}</el-button></template></el-table-column>
      </el-table>
      <el-button :disabled="offset === 0" @click="page(-50)">{{ t('上一页', 'Previous') }}</el-button>
      <el-button :disabled="jobs.length < 50" @click="page(50)">{{ t('下一页', 'Next') }}</el-button>
    </section>
  </div>
</template>
