import { t } from './locale'

export function graphStatusLabel(value?: string) {
  const labels: Record<string, string> = {
    pending: t('待处理', 'Queued'),
    processing: t('构建中', 'Building'),
    succeeded: t('已完成', 'Complete'),
    failed: t('失败', 'Failed'),
    expired: t('已过期', 'Expired'),
    ok: t('已命中', 'Matched'),
    no_hits: t('无图谱命中', 'No graph matches'),
    disabled: t('未启用', 'Disabled'),
    degraded: t('已降级', 'Fallback active'),
    not_requested: t('未使用图谱', 'Graph not requested'),
  }
  return value ? labels[value] || value : t('未构建', 'Not built')
}
