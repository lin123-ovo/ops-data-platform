<script setup lang="ts">
import { t } from '@/utils/locale'
import { graphStatusLabel } from '@/utils/graph'
import GraphEvidence from '@/components/GraphEvidence.vue'
import type { RetrievalMode } from '@/types/api'
import { Search } from '@element-plus/icons-vue'
import { onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'

import { searchRetrieval } from '@/api/modules/retrieval'
import { notifyError } from '@/api/http'
import type { RetrievalHitItem } from '@/types/api'
import { formatScore } from '@/utils/format'

const route = useRoute()
const loading = ref(false)
const retrievalMode = ref<RetrievalMode>('hybrid')
const knowledgeBase = ref('default')
const graphStatus = ref('')
const degradationReason = ref('')
const results = ref<RetrievalHitItem[]>([])
const form = reactive({
  query: String(route.query.keyword || ''),
  topK: 5,
})

function getSourceTagType(source: string | null | undefined) {
  switch (source) {
    case 'graph':
      return 'success'
    case 'hybrid':
      return 'danger'
    case 'vector':
      return 'primary'
    case 'bm25':
      return 'warning'
    case 'postgres_fallback':
      return 'info'
    default:
      return 'info'
  }
}

function getSourceLabel(source: string | null | undefined) {
  switch (source) {
    case 'graph':
      return t('图谱检索', 'Graph retrieval')
    case 'hybrid':
      return t('混合融合', 'Hybrid fusion')
    case 'vector':
      return t('向量检索', 'Vector retrieval')
    case 'bm25':
      return 'BM25'
    case 'postgres_fallback':
      return t('PostgreSQL 兜底', 'PostgreSQL fallback')
    default:
      return t('未标注', 'Unspecified')
  }
}

function formatRouteLabel(item: RetrievalHitItem) {
  if (item.retrieval_sources?.length) {
    return item.retrieval_sources.map(source => getSourceLabel(source)).join(' + ')
  }
  return getSourceLabel(item.retrieval_source)
}

async function runSearch() {
  if (!form.query.trim()) {
    return
  }

  loading.value = true
  try {
    const response = await searchRetrieval(form.query, form.topK, retrievalMode.value, knowledgeBase.value || undefined)
    results.value = response.items
    graphStatus.value = response.graph_status || ''
    degradationReason.value = response.degradation_reason || ''
  } catch (error) {
    notifyError(error, t('检索调试失败', 'Retrieval failed'))
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  if (form.query) {
    await runSearch()
  }
})
</script>

<template>
  <div class="page-grid">
    <section class="panel span-12 toolbar-card">
      <div>
        <div class="panel-kicker">Retrieval Debugger</div>
        <h2>{{ t('检索链路调试台', 'Retrieval console') }}</h2>
          <p class="page-description">
            {{ t('直接命中后端 `/retrieval/search`，查看 Milvus 向量检索、BM25 词法检索与 RRF 融合后的最终召回结果，并对照来源、分数、排名与切分元数据。', 'Inspect results from Milvus, BM25, and rank fusion, including source text, scores, ranks, and chunk metadata.') }}
          </p>
      </div>
      <div class="toolbar-actions wrap grow">
        <el-input v-model="form.query" :placeholder="t('输入检索问题或关键词', 'Enter a question or keyword')" @keyup.enter="runSearch">
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="retrievalMode" style="min-width:180px" :aria-label="t('检索模式', 'Retrieval mode')"><el-option :label="t('混合检索', 'Hybrid search')" value="hybrid" /><el-option :label="t('图谱增强检索', 'Graph-enhanced search')" value="graph_hybrid" /></el-select>
        <el-input v-model="knowledgeBase" :placeholder="t('知识库名称', 'Knowledge base')" :aria-label="t('知识库名称', 'Knowledge base')" />
        <el-input-number v-model="form.topK" :min="1" :max="20" />
        <el-button type="primary" @click="runSearch">{{ t('开始检索', 'Search') }}</el-button>
      </div>
    </section>

    <section class="panel span-12 section-card" v-loading="loading">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Results</div>
          <h3>{{ t('召回结果', 'Results') }}</h3>
        </div>
        <el-tag effect="dark">{{ results.length }} {{ t('条', 'results') }}</el-tag>
      </div>

      <el-alert v-if="degradationReason" :title="degradationReason" type="warning" :closable="false" />
      <el-tag v-if="graphStatus">{{ t('图谱', 'Graph') }} · {{ graphStatusLabel(graphStatus) }}</el-tag>
      <div v-if="results.length" class="result-grid">
        <article v-for="item in results" :key="`${item.chunk_id}-${item.chunk_index}`" class="result-card">
          <div class="result-card-top">
            <div class="result-card-title-block">
              <div class="stack-title">{{ item.filename || 'unknown' }}</div>
              <div class="stack-meta">
                <span>#{{ item.chunk_index ?? '--' }}</span>
                <span>{{ item.section_title || item.section_type || t('未命名片段', 'Untitled chunk') }}</span>
              </div>
            </div>
            <div class="result-card-badges">
              <el-tag :type="getSourceTagType(item.retrieval_source)">{{ getSourceLabel(item.retrieval_source) }}</el-tag>
              <el-tag type="success">{{ t('最终', 'Final') }} {{ formatScore(item.score) }}</el-tag>
            </div>
          </div>

          <div class="score-grid">
            <div class="score-item">
              <span class="score-label">{{ t('融合分数', 'Fusion score') }}</span>
              <strong>{{ item.fused_score != null ? formatScore(item.fused_score) : '--' }}</strong>
            </div>
            <div class="score-item">
              <span class="score-label">{{ t('向量分数', 'Vector score') }}</span>
              <strong>{{ item.vector_score != null ? formatScore(item.vector_score) : '--' }}</strong>
            </div>
            <div class="score-item">
              <span class="score-label">{{ t('BM25 分数', 'BM25 score') }}</span>
              <strong>{{ item.bm25_score != null ? formatScore(item.bm25_score) : '--' }}</strong>
            </div>
            <div class="score-item">
              <span class="score-label">{{ t('融合排名', 'Fusion rank') }}</span>
              <strong>#{{ item.rank_fused ?? '--' }}</strong>
            </div>
          </div>

          <div class="pill-row">
            <span class="meta-pill">{{ t('命中路径', 'Retrieval path') }} · {{ formatRouteLabel(item) }}</span>
            <span class="meta-pill">vector rank: {{ item.rank_vector ?? '--' }}</span>
            <span class="meta-pill">bm25 rank: {{ item.rank_bm25 ?? '--' }}</span>
          </div>

          <GraphEvidence :paths="item.graph_paths" />
          <p class="stack-text">{{ item.content }}</p>

          <div class="pill-row">
            <span class="meta-pill">splitter: {{ item.splitter_name || '--' }}</span>
            <span class="meta-pill">parser: {{ item.parser_name || '--' }}</span>
            <span class="meta-pill">page: {{ item.page_number ?? '--' }}</span>
            <span class="meta-pill">file_type: {{ item.file_type || '--' }}</span>
          </div>
        </article>
      </div>
      <el-empty v-else :description="t('输入检索问题后查看召回结果', 'Enter a search query to see results')" />
    </section>
  </div>
</template>
