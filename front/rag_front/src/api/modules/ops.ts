import { http } from '@/api/http'

export const opsEnabled = import.meta.env.VITE_OPS_ENABLED === 'true'

export interface OpsPeriod {
  start_date: string
  end_date: string
}

export interface OpsQuery extends OpsPeriod {
  service_id?: string
}

export type DetailKind = 'breached' | 'due'

export interface OpsService {
  service_id: string
  name: string
  team: string | null
}

interface ServiceMetrics extends OpsService {
  due_count: number
  breached_count: number
  compliance_rate: number | null
  rank: number | null
}

interface MetricCatalog {
  items: { metric_id: string; name: string; formula: string; scope: string; source_tables: string[] }[]
  sync_interval_seconds: number
  last_success_at: string | null
}

export interface MetricSnapshot extends OpsPeriod {
  metric_id: 'sla_breached_ticket_count'
  status: 'ok' | 'not_ready' | 'unavailable'
  count: number | null
  due_count?: number | null
  compliance_rate?: number | null
  service_id?: string | null
  service?: OpsService | null
  service_breakdown?: ServiceMetrics[]
  data_version: string | null
  last_success_at: string | null
}

interface SyncRun {
  id: string
  trigger: 'manual' | 'scheduled'
  status: 'queued' | 'running' | 'succeeded' | 'failed'
  queued_at: string
  started_at: string | null
  finished_at: string | null
  rows_read: number
  rows_written: number
  error: string | null
}

interface SyncRunList {
  items: SyncRun[]
  last_success_at: string | null
}

export interface TicketPage {
  data_version: string
  total: number
  items: {
    ticket_id: string
    title: string
    created_at: string
    sla_due_at: string
    resolved_at: string | null
    service_name: string
    sla_met: boolean
  }[]
}

const metricPath = '/ops/metrics/sla-breached-tickets'

export async function fetchOpsMetric(query: OpsQuery): Promise<MetricSnapshot> {
  const { data } = await http.get<MetricSnapshot>('/ops/metrics/sla', { params: query })
  return data
}

export async function fetchOpsServices(): Promise<OpsService[]> {
  const { data } = await http.get<OpsService[]>('/ops/services')
  return data
}

export async function fetchMetricCatalog(language: 'zh' | 'en' = 'zh'): Promise<MetricCatalog> {
  const { data } = await http.get<MetricCatalog>('/ops/metrics/catalog', { params: { language } })
  return data
}

export async function fetchOpsTickets(
  query: OpsQuery, version: string, offset: number, limit: number, kind: DetailKind,
) {
  const { data } = await http.get<TicketPage>(metricPath + '/items', {
    params: { ...query, data_version: version, offset, limit, kind },
  })
  return data
}

export async function fetchSyncRuns(): Promise<SyncRunList> {
  const { data } = await http.get<SyncRunList>('/ops/sync-runs')
  return data
}

export async function requestOpsSync(): Promise<SyncRun> {
  const { data } = await http.post<SyncRun>('/ops/sync-runs')
  return data
}
