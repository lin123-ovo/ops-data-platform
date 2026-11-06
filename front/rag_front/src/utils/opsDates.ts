import { language } from '@/utils/locale'
import type { OpsPeriod } from '@/api/modules/ops'

const hkDate = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Hong_Kong' })
const timestampOptions: Intl.DateTimeFormatOptions = {
  timeZone: 'Asia/Hong_Kong',
  month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
}
const timestamps = {
  zh: new Intl.DateTimeFormat('zh-HK', timestampOptions),
  en: new Intl.DateTimeFormat('en-GB', { ...timestampOptions, month: 'short' }),
}
const dayMs = 86_400_000

export function lastFullWeek(now = new Date()): OpsPeriod {
  const today = new Date(hkDate.format(now) + 'T00:00:00Z')
  const sinceMonday = (today.getUTCDay() + 6) % 7
  const monday = today.getTime() - sinceMonday * dayMs
  return {
    start_date: new Date(monday - 7 * dayMs).toISOString().slice(0, 10),
    end_date: new Date(monday).toISOString().slice(0, 10),
  }
}

export function formatOpsDateTime(value: string): string {
  return timestamps[language.value].format(new Date(value))
}
