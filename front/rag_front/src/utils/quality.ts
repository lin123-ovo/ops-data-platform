export interface QualityReport {
  checked: boolean
  warnings: string[]
}

const REPORT_PREFIX = 'quality_report_v1='

/** 旧文档或损坏报告均显示未检测，不影响文档查看。 */
export function readQualityReport(summary: string | null | undefined): QualityReport {
  const line = (summary || '').split('\n').reverse().find(item => item.startsWith(REPORT_PREFIX))
  if (!line) return { checked: false, warnings: [] }
  try {
    const report: unknown = JSON.parse(line.slice(REPORT_PREFIX.length))
    if (
      typeof report === 'object' && report !== null &&
      'checked' in report && typeof report.checked === 'boolean' &&
      'warnings' in report && Array.isArray(report.warnings) &&
      report.warnings.every((item: unknown) => typeof item === 'string')
    ) {
      return { checked: report.checked, warnings: report.warnings }
    }
  } catch {
    // 报告是历史记录的可选附加信息；异常时回退到未检测。
  }
  return { checked: false, warnings: [] }
}
