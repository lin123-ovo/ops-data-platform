<script setup lang="ts">
import { demoLabel, language, t } from '@/utils/locale'
import { isAxiosError } from 'axios'
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { notifyError } from '@/api/http'
import { fetchMetricCatalog, fetchOpsMetric, fetchOpsServices, fetchOpsTickets, fetchSyncRuns, requestOpsSync } from '@/api/modules/ops'
import type { DetailKind, MetricSnapshot, OpsPeriod, OpsService, TicketPage } from '@/api/modules/ops'
import OpsMetricCard from '@/components/OpsMetricCard.vue'
import { formatOpsDateTime, lastFullWeek } from '@/utils/opsDates'

const route = useRoute()
const router = useRouter()
const preset = lastFullWeek()
const period = ref<OpsPeriod>({
  start_date: typeof route.query.start_date === 'string' ? route.query.start_date : preset.start_date,
  end_date: typeof route.query.end_date === 'string' ? route.query.end_date : preset.end_date,
})
const serviceId = ref(typeof route.query.service_id === 'string' ? route.query.service_id : '')
const services = ref<OpsService[]>([])
const catalog = ref<Awaited<ReturnType<typeof fetchMetricCatalog>>>()
const metric = ref<MetricSnapshot>()
const detailKind = ref<DetailKind>(route.query.kind === 'due' ? 'due' : 'breached')
const detailServiceId = ref<string | null>(serviceId.value || null)
const detailService = computed(() => {
  const name = services.value.find((item) => item.service_id === detailServiceId.value)?.name
  return name ? demoLabel(name) : t('全部服务', 'All services')
})
const details = ref<TicketPage>()
const stale = ref(false)
const loadingMetric = ref(false)
const loadingRuns = ref(false)
const submitting = ref(false)
const runsError = ref(false)
const runList = ref<Awaited<ReturnType<typeof fetchSyncRuns>>>()
const page = ref(1)
const pageSize = 20
let timer: ReturnType<typeof setInterval> | undefined
const activeRun = computed(() => runList.value?.items.some(({ status }) => status === 'queued' || status === 'running'))
const statusLabels = computed(() => ({ queued: t('排队中', 'Queued'), running: t('同步中', 'Syncing'), succeeded: t('成功', 'Succeeded'), failed: t('失败', 'Failed') }))

async function openDetails(kind: DetailKind, scope: string | null) {
  if (loadingMetric.value || stale.value) return
  detailKind.value = kind
  detailServiceId.value = scope
  await changePage(1)
}

async function loadMetadata() {
  const requestedLanguage = language.value
  const [serviceResult, catalogResult] = await Promise.allSettled([
    fetchOpsServices(), fetchMetricCatalog(requestedLanguage),
  ])
  if (requestedLanguage !== language.value) return
  if (serviceResult.status === 'fulfilled') services.value = serviceResult.value
  else notifyError(serviceResult.reason, t('读取服务目录失败', 'Could not load the service catalog'))
  if (catalogResult.status === 'fulfilled') catalog.value = catalogResult.value
  else notifyError(catalogResult.reason, t('读取指标目录失败', 'Could not load the metric catalog'))
}

async function loadDetails() {
  const current = metric.value
  if (!current || current.status !== 'ok' || current.data_version === null || stale.value) return
  details.value = undefined
  try {
    details.value = await fetchOpsTickets(
      {
        start_date: current.start_date, end_date: current.end_date,
        service_id: detailServiceId.value ?? undefined,
      },
      current.data_version, (page.value - 1) * pageSize, pageSize, detailKind.value,
    )
  } catch (error) {
    if (isAxiosError(error) && error.response?.status === 409) {
      stale.value = true
    } else {
      notifyError(error, t('读取工单明细失败', 'Could not load ticket details'))
    }
  }
}

async function loadMetric(expectedVersion?: string) {
  if (loadingMetric.value) return
  loadingMetric.value = true
  details.value = undefined
  stale.value = false
  page.value = 1
  const range = { ...period.value, service_id: serviceId.value || undefined }
  detailServiceId.value = serviceId.value || null
  try {
    const current = await fetchOpsMetric(range)
    if (expectedVersion !== undefined && current.data_version !== expectedVersion) {
      metric.value = undefined
      stale.value = true
      return
    }
    metric.value = current
    await loadDetails()
  } catch (error) {
    const invalidRange = isAxiosError(error) && error.response?.status === 422
    notifyError(error, invalidRange ? t('请选择已结束且最长 90 天的日期区间', 'Select a completed date range of up to 90 days') : t('取数失败', 'Could not load metrics'))
    metric.value = invalidRange ? undefined : {
      ...range, metric_id: 'sla_breached_ticket_count', status: 'unavailable',
      count: null, data_version: null, last_success_at: null,
    }
  } finally {
    loadingMetric.value = false
  }
}

async function refreshMetric() {
  if (route.query.data_version !== undefined) {
    await router.replace({ name: 'ops', query: {
      ...period.value, service_id: serviceId.value || undefined, kind: detailKind.value,
    } })
  }
  await Promise.all([loadMetric(), loadMetadata()])
}

async function changePage(next: number) {
  if (loadingMetric.value) return
  page.value = next
  loadingMetric.value = true
  try {
    await loadDetails()
  } finally {
    loadingMetric.value = false
  }
}

async function refreshRuns() {
  if (loadingRuns.value) return
  loadingRuns.value = true
  try {
    runList.value = await fetchSyncRuns()
    runsError.value = false
  } catch {
    runsError.value = true
  } finally {
    loadingRuns.value = false
  }
}

async function submitSync() {
  submitting.value = true
  try {
    await requestOpsSync()
    await refreshRuns()
  } catch (error) {
    notifyError(error, t('提交同步失败', 'Could not start the sync'))
  } finally {
    submitting.value = false
  }
}

function useLastWeek() {
  period.value = lastFullWeek()
  void refreshMetric()
}

onMounted(() => {
  void refreshRuns()
  void loadMetadata()
  void loadMetric(typeof route.query.data_version === 'string' ? route.query.data_version : undefined)
  timer = setInterval(() => void refreshRuns(), 2500)
})
watch(language, () => {
  catalog.value = undefined
  void loadMetadata()
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <div class="page-grid">
    <section class="panel span-12 section-card">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">DATA SERVICE</div>
          <h3>{{ t('工单 SLA 指标', 'Ticket SLA metrics') }}</h3>
        </div>
      </div>
      <p class="page-description">
        {{ t('按 SLA 截止日期统计。到期仍未解决或晚于截止时间解决的工单计入超时。', 'Metrics use the SLA due date. Unresolved tickets past the deadline and tickets resolved late count as breaches.') }}
      </p>
      <div class="toolbar-actions wrap ops-filters">
        <el-button :disabled="loadingMetric" @click="useLastWeek">{{ t('上周', 'Last week') }}</el-button>
        <el-date-picker v-model="period.start_date" :clearable="false" type="date" value-format="YYYY-MM-DD"
          :placeholder="t('开始日期', 'Start date')" :aria-label="t('开始日期', 'Start date')" />
        <el-date-picker v-model="period.end_date" :clearable="false" type="date" value-format="YYYY-MM-DD"
          :placeholder="t('结束日期（不含）', 'End date (exclusive)')" :aria-label="t('结束日期（不含）', 'End date (exclusive)')" />
        <el-select v-model="serviceId" :aria-label="t('服务筛选', 'Filter by service')" :empty-values="[null, undefined]" class="ops-service-select" :disabled="loadingMetric">
          <el-option :label="t('全部服务', 'All services')" value="" />
          <el-option v-for="item in services" :key="item.service_id" :label="demoLabel(item.name)" :value="item.service_id" />
        </el-select>
        <el-button type="primary" :loading="loadingMetric" @click="refreshMetric">{{ t('查询 / 刷新指标', 'Refresh metrics') }}</el-button>
      </div>
      <el-alert v-if="stale" :title="t('数据版本已更新，请刷新指标后再查看明细', 'The data version has changed. Refresh the metrics to view ticket details.')" type="warning" :closable="false" />
      <OpsMetricCard v-if="metric" :metric="metric" :disable-details="loadingMetric || stale" @details="openDetails" />
      <div v-if="details" class="ops-details">
        <h4>{{ detailService }} · {{ detailKind === 'due' ? t('到期工单明细', 'Due ticket details') : t('超时工单明细', 'Breached ticket details') }}</h4>
        <el-table :data="details.items" :empty-text="t('区间内没有符合条件的工单', 'No matching tickets in this date range')">
          <el-table-column prop="ticket_id" :label="t('工单 ID', 'Ticket ID')" min-width="190" />
          <el-table-column :label="t('标题', 'Title')" min-width="160">
            <template #default="{ row }">{{ demoLabel(row.title) }}</template>
          </el-table-column>
          <el-table-column :label="t('服务', 'Service')" min-width="130">
            <template #default="{ row }">{{ demoLabel(row.service_name) }}</template>
          </el-table-column>
          <el-table-column :label="t('是否达标', 'SLA status')" min-width="100">
            <template #default="{ row }">{{ row.sla_met ? t('达标', 'Met') : t('超时', 'Breached') }}</template>
          </el-table-column>
          <el-table-column :label="t('SLA 截止', 'SLA deadline')" min-width="160">
            <template #default="{ row }">{{ formatOpsDateTime(row.sla_due_at) }}</template>
          </el-table-column>
          <el-table-column :label="t('解决时间', 'Resolved at')" min-width="160">
            <template #default="{ row }">{{ row.resolved_at ? formatOpsDateTime(row.resolved_at) : t('未解决', 'Unresolved') }}</template>
          </el-table-column>
        </el-table>
        <el-pagination v-if="details.total > pageSize" :current-page="page" :page-size="pageSize"
          :total="details.total" :disabled="loadingMetric" layout="prev, pager, next" @current-change="changePage" />
      </div>
    </section>

    <section v-if="catalog" class="panel span-12 section-card">
      <h3>{{ t('指标目录', 'Metric catalog') }}</h3>
      <p class="page-description">
        {{ t('每', 'Checks for changes every') }} {{ catalog.sync_interval_seconds }} {{ t('秒检查增量', 'seconds') }}
        · {{ t('最近成功同步', 'Last successful sync') }} {{ catalog.last_success_at ? formatOpsDateTime(catalog.last_success_at) : t('暂无', 'Unavailable') }}
      </p>
      <el-table :data="catalog.items">
        <el-table-column prop="name" :label="t('指标', 'Metric')" min-width="140" />
        <el-table-column prop="formula" :label="t('公式', 'Formula')" min-width="250" />
        <el-table-column prop="scope" :label="t('统计口径', 'Definition')" min-width="260" />
        <el-table-column :label="t('来源表', 'Source tables')" min-width="230">
          <template #default="{ row }">{{ row.source_tables.join(t('、', ', ')) }}</template>
        </el-table-column>
      </el-table>
    </section>

    <section class="panel span-12 section-card">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">DATA INTEGRATION</div>
          <h3>{{ t('工单与服务目录同步', 'Ticket and service catalog sync') }}</h3>
          <p class="page-description">
            {{ t('最近成功同步', 'Last successful sync') }} {{ runList?.last_success_at ? formatOpsDateTime(runList.last_success_at) : t('暂无', 'Unavailable') }}
          </p>
        </div>
        <el-button :loading="submitting" :disabled="activeRun" @click="submitSync">{{ t('立即同步', 'Sync now') }}</el-button>
      </div>
      <el-alert v-if="runsError" :title="t('同步记录暂不可用，请检查后端与迁移状态', 'Sync history is unavailable. Check the backend and database migrations.')" type="warning" :closable="false" />
      <el-table :data="runList?.items || []" :empty-text="t('暂无同步任务', 'No sync runs yet')">
        <el-table-column :label="t('提交时间', 'Submitted at')" min-width="165">
          <template #default="{ row }">{{ formatOpsDateTime(row.queued_at) }}</template>
        </el-table-column>
        <el-table-column :label="t('触发方式', 'Trigger')" min-width="95">
          <template #default="{ row }">{{ row.trigger === 'manual' ? t('手动', 'Manual') : t('定时', 'Scheduled') }}</template>
        </el-table-column>
        <el-table-column :label="t('状态', 'Status')" min-width="100">
          <template #default="{ row }">{{ statusLabels[row.status as keyof typeof statusLabels] }}</template>
        </el-table-column>
        <el-table-column prop="rows_read" :label="t('读取', 'Read')" min-width="75" />
        <el-table-column prop="rows_written" :label="t('写入', 'Written')" min-width="75" />
        <el-table-column :label="t('失败原因', 'Error')" min-width="220">
          <template #default="{ row }">
            {{ row.error && (language === 'en' ? 'Sync failed. Check the worker logs.' : row.error) }}
          </template>
        </el-table-column>
      </el-table>
    </section>
  </div>
</template>

<style scoped>
.ops-filters { margin: 0.75rem 0; }
.ops-service-select { width: 165px; }
.ops-details { margin-top: 1rem; }
</style>
