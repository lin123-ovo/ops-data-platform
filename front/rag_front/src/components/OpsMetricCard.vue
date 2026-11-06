<script setup lang="ts">
import { demoLabel, t } from '@/utils/locale'
import { useRouter } from 'vue-router'
import type { DetailKind, MetricSnapshot } from '@/api/modules/ops'
import { formatOpsDateTime } from '@/utils/opsDates'

const props = defineProps<{ metric: MetricSnapshot; linkToDetails?: boolean; disableDetails?: boolean }>()
const emit = defineEmits<{ details: [kind: DetailKind, serviceId: string | null] }>()
const router = useRouter()
const percent = new Intl.NumberFormat('zh-HK', { style: 'percent', maximumFractionDigits: 2 })

function formatRate(rate?: number | null) {
  return rate == null ? t('无数据', 'No data') : percent.format(rate)
}

function openDetails(kind: DetailKind, serviceId = props.metric.service_id ?? null) {
  if (props.linkToDetails) {
    void router.push({ name: 'ops', query: {
      start_date: props.metric.start_date,
      end_date: props.metric.end_date,
      data_version: props.metric.data_version,
      service_id: serviceId ?? undefined,
      kind,
    } })
  } else {
    emit('details', kind, serviceId)
  }
}
</script>

<template>
  <div class="ops-metric" aria-live="polite">
    <strong>{{ metric.due_count == null ? t('SLA 超时工单', 'SLA breaches') : metric.service ? demoLabel(metric.service.name) : t('全部服务 SLA', 'All services SLA') }}</strong>
    <p v-if="metric.service?.team" class="ops-caption">{{ t('责任团队', 'Responsible team') }} {{ demoLabel(metric.service.team) }}</p>
    <div v-if="metric.status !== 'ok'" class="ops-value">
      {{ metric.status === 'not_ready' ? t('数据未就绪', 'Data not ready') : t('取数失败', 'Could not load metrics') }}
    </div>
    <template v-else-if="metric.due_count == null">
      <div class="ops-value">{{ metric.count }} <small>{{ t('单', 'tickets') }}</small></div>
      <button v-if="linkToDetails" type="button" class="ops-link" @click.stop="openDetails('breached')">
        {{ t('查看工单明细', 'View ticket details') }}
      </button>
    </template>
    <div v-else class="ops-values">
      <button type="button" :disabled="disableDetails" @click.stop="openDetails('due')">
        <span>{{ t('到期工单数', 'Tickets due') }}</span><strong>{{ metric.due_count }}{{ t(' 单', '') }}</strong>
      </button>
      <button type="button" :disabled="disableDetails" @click.stop="openDetails('breached')">
        <span>{{ t('超时工单数', 'SLA breaches') }}</span><strong>{{ metric.count }}{{ t(' 单', '') }}</strong>
      </button>
      <button type="button" :disabled="disableDetails" @click.stop="openDetails('due')">
        <span>{{ t('SLA 达标率', 'SLA compliance') }}</span><strong>{{ formatRate(metric.compliance_rate) }}</strong>
      </button>
    </div>
    <p class="ops-caption">{{ metric.start_date }} {{ t('至', 'to') }} {{ metric.end_date }} {{ t('（不含结束日，香港时间）', '(end date excluded, Hong Kong time)') }}</p>
    <p v-if="metric.last_success_at" class="ops-caption">
      {{ t('数据版本', 'Data version') }} {{ metric.data_version }} · {{ t('最近同步', 'Last sync') }} {{ formatOpsDateTime(metric.last_success_at) }}
    </p>
    <div v-if="metric.service_breakdown?.length" class="ops-ranking">
      <h4>{{ t('服务排名', 'Service ranking') }} <small>{{ t('按 SLA 达标率从低到高', 'Lowest compliance first') }}</small></h4>
      <table>
        <thead><tr><th>{{ t('排名', 'Rank') }}</th><th>{{ t('服务', 'Service') }}</th><th>{{ t('责任团队', 'Responsible team') }}</th><th>{{ t('到期', 'Due') }}</th><th>{{ t('超时', 'Breaches') }}</th><th>{{ t('达标率', 'Compliance') }}</th></tr></thead>
        <tbody>
          <tr v-for="row in metric.service_breakdown" :key="row.service_id">
            <td>{{ row.rank ?? '—' }}</td>
            <th scope="row">{{ demoLabel(row.name) }}</th>
            <td>{{ row.team ? demoLabel(row.team) : '—' }}</td>
            <td><button class="ops-link" type="button" :disabled="disableDetails"
              :aria-label="`${demoLabel(row.name)}${t('到期工单明细', ' due ticket details')}`" @click.stop="openDetails('due', row.service_id)">{{ row.due_count }}</button></td>
            <td><button class="ops-link" type="button" :disabled="disableDetails"
              :aria-label="`${demoLabel(row.name)}${t('超时工单明细', ' breached ticket details')}`" @click.stop="openDetails('breached', row.service_id)">{{ row.breached_count }}</button></td>
            <td><button class="ops-link" type="button" :disabled="disableDetails"
              :aria-label="`${demoLabel(row.name)}${t('达标率工单明细', ' SLA compliance ticket details')}`" @click.stop="openDetails('due', row.service_id)">{{ formatRate(row.compliance_rate) }}</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.ops-metric { padding: 1rem; border: 1px solid var(--el-border-color); border-radius: 0.75rem; }
.ops-value { margin-top: 0.4rem; font-size: 1.55rem; font-weight: 700; }
.ops-value small, .ops-ranking small { font-size: 0.8rem; font-weight: normal; }
.ops-caption { margin: 0.35rem 0; font-size: 0.8rem; }
.ops-values { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.6rem 0; }
.ops-values button { padding: 0.55rem 0.75rem; border: 1px solid var(--el-border-color);
  border-radius: 0.4rem; background: transparent; color: inherit; text-align: left; cursor: pointer; }
.ops-values button strong { display: block; margin-top: 0.15rem; }
.ops-ranking { overflow-x: auto; }
.ops-ranking h4 { margin: 0.8rem 0 0.35rem; }
.ops-ranking table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.ops-ranking th, .ops-ranking td { text-align: left; padding: 0.35rem; border-bottom: 1px solid var(--el-border-color); }
.ops-link { padding: 0; border: 0; background: transparent; color: var(--el-color-primary);
  text-decoration: underline; cursor: pointer; }
button:disabled { opacity: 0.65; cursor: default; }
</style>
