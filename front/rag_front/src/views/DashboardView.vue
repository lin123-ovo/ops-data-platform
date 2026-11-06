<script setup lang="ts">
import { demoLabel, t } from '@/utils/locale'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { fetchOpsMetric, opsEnabled } from '@/api/modules/ops'
import type { MetricSnapshot } from '@/api/modules/ops'
import { formatOpsDateTime, lastFullWeek } from '@/utils/opsDates'
import { useAppStore } from '@/stores/app'
import { useChatStore } from '@/stores/chat'
import { useDocumentStore } from '@/stores/documents'
import { formatDateTime, truncateText } from '@/utils/format'

const router = useRouter()
const appStore = useAppStore()
const documentStore = useDocumentStore()
const chatStore = useChatStore()

// 看板从 Pinia 的文档、会话、健康状态生成统计；这里只展示数据，不执行 RAG 算法。
const stats = computed(() => [
  {
    label: t('知识文档', 'Knowledge documents'),
    value: documentStore.documents.length,
    description: t('已入库文档总数', 'Documents in the knowledge base'),
  },
  {
    label: t('切分 Chunk', 'Document chunks'),
    value: documentStore.totalChunks,
    description: t('当前文档切分总量', 'Total chunks across documents'),
  },
  {
    label: t('最近会话', 'Recent conversations'),
    value: chatStore.sessions.length,
    description: t('已记录的 Agent 会话', 'Recently loaded agent conversations'),
  },
  {
    label: t('后端状态', 'Backend status'),
    value: appStore.backendReady ? t('在线', 'Online') : t('检查中', 'Checking'),
    description: t('Postgres / Redis / Milvus 健康探测', 'PostgreSQL, Redis, and Milvus health'),
  },
])

const latestDocuments = computed(() => documentStore.documents.slice(0, 5))
const latestSessions = computed(() => chatStore.sessions.slice(0, 5))

const opsSnapshot = ref<MetricSnapshot>()
const opsLoading = ref(false)
const opsError = ref(false)
const opsPeriod = ref(lastFullWeek())
const percent = new Intl.NumberFormat('zh-HK', { style: 'percent', maximumFractionDigits: 2 })
const lowestService = computed(() => opsSnapshot.value?.service_breakdown?.find((item) => item.rank === 1))

function formatRate(value?: number | null) {
  return value == null ? t('无数据', 'No data') : percent.format(value)
}

async function refreshOpsOverview() {
  if (!opsEnabled || opsLoading.value) return
  opsLoading.value = true
  opsError.value = false
  opsPeriod.value = lastFullWeek()
  try {
    opsSnapshot.value = await fetchOpsMetric(opsPeriod.value)
  } catch {
    opsSnapshot.value = undefined
    opsError.value = true
  } finally {
    opsLoading.value = false
  }
}

function openOps() {
  void router.push({ name: 'ops', query: { ...opsPeriod.value } })
}

function openServiceDetails() {
  const snapshot = opsSnapshot.value
  const service = lowestService.value
  if (!snapshot?.data_version || !service) return
  void router.push({ name: 'ops', query: {
    start_date: snapshot.start_date,
    end_date: snapshot.end_date,
    service_id: service.service_id,
    data_version: snapshot.data_version,
    kind: 'due',
  } })
}

async function refreshOverview() {
  await Promise.all([refreshOpsOverview(), chatStore.loadSessions()])
}

defineExpose({ refreshOverview })

onMounted(async () => {
  await Promise.all([
    refreshOpsOverview(),
    chatStore.sessions.length ? undefined : chatStore.loadSessions(),
  ])
})
</script>

<template>
  <div class="page-grid">
    <section class="hero-card panel span-12">
      <div>
        <h2 class="hero-title">{{ opsEnabled ? t('知识检索与运维分析', 'Knowledge Retrieval and SLA Analytics') : t('知识检索与 Agent 问答', 'Knowledge Retrieval and Agent Chat') }}</h2>
        <p class="hero-description">
          {{ opsEnabled
            ? t('查看工单 SLA 和服务表现，管理知识文档，并通过 Agent 追溯手册原文。', 'Track service SLA performance, manage knowledge documents, and trace answers to runbook evidence.')
            : t('管理知识文档、检查检索结果，并通过 Agent 追溯回答来源。', 'Manage knowledge documents, inspect retrieval results, and trace answers to their sources.') }}
        </p>
      </div>
      <div class="hero-actions">
        <el-button v-if="opsEnabled" type="primary" size="large" @click="openOps">{{ t('查看运维数据', 'Open Operations') }}</el-button>
        <el-button :type="opsEnabled ? 'default' : 'primary'" size="large" @click="router.push({ name: 'ingest' })">
          {{ t('导入文档', 'Import documents') }}
        </el-button>
        <el-button size="large" @click="router.push({ name: 'chat' })">{{ t('Agent 对话', 'Agent Chat') }}</el-button>
      </div>
    </section>

    <section v-if="opsEnabled" class="panel span-12 section-card" aria-labelledby="ops-overview-title">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">{{ t('运维数据', 'Operations') }}</div>
          <h3 id="ops-overview-title">{{ t('上周工单 SLA', 'Last Week’s SLA') }}</h3>
          <p class="ops-meta">
            {{ opsPeriod.start_date }} {{ t('至', 'to') }} {{ opsPeriod.end_date }} {{ t('（不含结束日，香港时间）', '(end date excluded, Hong Kong time)') }}
          </p>
        </div>
        <el-tag effect="plain" type="info">{{ t('合成演示数据', 'Synthetic demo data') }}</el-tag>
      </div>
      <div aria-live="polite" :aria-busy="opsLoading">
        <p v-if="opsLoading" class="ops-meta">{{ t('正在读取 SLA 指标…', 'Loading SLA metrics…') }}</p>
        <el-alert v-else-if="opsError || opsSnapshot?.status === 'unavailable'"
          :title="t('暂时无法读取 SLA 指标，请刷新重试', 'Could not load SLA metrics. Refresh to try again.')" type="warning" :closable="false" />
        <p v-else-if="opsSnapshot?.status !== 'ok'" class="ops-meta">{{ t('数据未就绪，等待首次同步成功。', 'Data will appear after the first successful sync.') }}</p>
        <template v-else>
          <dl class="ops-summary">
            <div>
              <dt>{{ t('到期工单', 'Tickets due') }}</dt>
              <dd>{{ opsSnapshot.due_count ?? '—' }}<small v-if="opsSnapshot.due_count != null">{{ t(' 单', ' tickets') }}</small></dd>
            </div>
            <div>
              <dt>{{ t('超时工单', 'SLA breaches') }}</dt>
              <dd>{{ opsSnapshot.count ?? '—' }}<small v-if="opsSnapshot.count != null">{{ t(' 单', ' tickets') }}</small></dd>
            </div>
            <div>
              <dt>{{ t('SLA 达标率', 'SLA compliance') }}</dt>
              <dd>{{ formatRate(opsSnapshot.compliance_rate) }}</dd>
            </div>
            <div>
              <dt>{{ t('达标率最低的服务', 'Lowest-ranked service') }}</dt>
              <dd v-if="lowestService" class="ops-service">
                <el-button link type="primary" @click="openServiceDetails">{{ demoLabel(lowestService.name) }}</el-button>
                <small>{{ formatRate(lowestService.compliance_rate) }}</small>
              </dd>
              <dd v-else class="ops-service">{{ t('暂无排名', 'No ranked services') }}</dd>
            </div>
          </dl>
          <p v-if="opsSnapshot.last_success_at" class="ops-meta">
            {{ t('最近成功同步', 'Last successful sync') }} {{ formatOpsDateTime(opsSnapshot.last_success_at) }}
          </p>
        </template>
      </div>
    </section>

    <section v-for="item in stats" :key="item.label" class="metric-card panel span-3">
      <div class="metric-label">{{ item.label }}</div>
      <div class="metric-value">{{ item.value }}</div>
      <div class="metric-description">{{ item.description }}</div>
    </section>

    <section class="panel span-6 section-card">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Documents</div>
          <h3>{{ t('最近文档', 'Recent documents') }}</h3>
        </div>
        <el-button text @click="router.push({ name: 'documents' })">{{ t('查看全部', 'View all') }}</el-button>
      </div>
      <div v-if="latestDocuments.length" class="stack-list">
        <article v-for="item in latestDocuments" :key="item.id" class="stack-item">
          <div>
            <div class="stack-title">{{ item.filename }}</div>
            <div class="stack-meta">
              <span>{{ item.file_type }}</span>
              <span>{{ item.chunk_count }} chunks</span>
              <span>{{ formatDateTime(item.updated_at) }}</span>
            </div>
          </div>
          <el-button size="small" @click="router.push({ name: 'chunks', query: { documentId: item.id } })">{{ t('查看切分', 'View chunks') }}</el-button>
        </article>
      </div>
      <el-empty v-else :description="t('暂无文档数据', 'No documents yet')" />
    </section>

    <section class="panel span-6 section-card">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Sessions</div>
          <h3>{{ t('最近会话', 'Recent conversations') }}</h3>
        </div>
        <el-button text @click="router.push({ name: 'chat' })">{{ t('进入对话', 'Open chat') }}</el-button>
      </div>
      <div v-if="latestSessions.length" class="stack-list">
        <article v-for="item in latestSessions" :key="item.session_id" class="stack-item wide">
          <div>
            <div class="stack-title">{{ truncateText(item.latest_question, 46) }}</div>
            <div class="stack-meta">
              <span>{{ item.message_count }} {{ t('轮消息', 'turns') }}</span>
              <span>{{ formatDateTime(item.updated_at) }}</span>
            </div>
            <p class="stack-text">{{ truncateText(item.latest_answer || t('暂无回答', 'No response yet'), 100) }}</p>
          </div>
        </article>
      </div>
      <el-empty v-else :description="t('暂无会话记录', 'No conversations yet')" />
    </section>
  </div>
</template>

<style scoped>
.hero-card { min-height: auto; align-items: center; }
.hero-title { margin-top: 0; }
.hero-actions { flex-wrap: wrap; }
.hero-actions :deep(.el-button + .el-button) { margin-left: 0; }
.ops-meta { margin: 0.4rem 0 0; color: var(--app-text-secondary); }
.ops-summary {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin: 0 0 0.7rem;
}
.ops-summary > div {
  padding: 0.85rem 1rem;
  border-radius: 12px;
  border: 1px solid var(--el-border-color-light);
}
.ops-summary dt { color: var(--app-text-secondary); font-size: 0.9rem; }
.ops-summary dd { margin: 0.35rem 0 0; font-size: 1.45rem; font-weight: 700; }
.ops-summary small { font-size: 0.85rem; font-weight: 400; }
.ops-summary .ops-service { display: flex; align-items: baseline; gap: 0.45rem; flex-wrap: wrap; font-size: 1rem; }
@media (max-width: 1250px) {
  .hero-card { align-items: flex-start; }
  .ops-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
