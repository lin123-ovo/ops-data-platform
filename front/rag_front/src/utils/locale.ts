import { ref } from 'vue'

export type Language = 'zh' | 'en'

const storageKey = 'rag-agent-language'
export const language = ref<Language>(localStorage.getItem(storageKey) === 'en' ? 'en' : 'zh')

export function setLanguage(value: Language) {
  language.value = value
  localStorage.setItem(storageKey, value)
}

export function t(chinese: string, english: string): string {
  return language.value === 'en' ? english : chinese
}

const demoLabels: Record<string, string> = {
  未匹配服务: 'Unmatched service',
  采集网关: 'Data Collection Gateway',
  网络接入: 'Network Access',
  告警平台: 'Alerting Platform',
  平台运维组: 'Platform Operations',
  网络运维组: 'Network Operations',
  网络值班组: 'Network On-call',
  监控运维组: 'Monitoring Operations',
  网络巡检告警: 'Network health alert',
  采集网关故障: 'Data collection gateway outage',
  应用接口延迟: 'Application API latency',
  备份任务异常: 'Backup job failure',
  日志采集恢复: 'Log collection recovery',
  新增演示超时工单: 'New overdue demo ticket',
}

export function demoLabel(value: string): string {
  return language.value === 'en' && Object.hasOwn(demoLabels, value) ? demoLabels[value]! : value
}
