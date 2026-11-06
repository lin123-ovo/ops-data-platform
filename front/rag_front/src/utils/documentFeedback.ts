import { ElMessage } from 'element-plus'
import { t } from './locale'
import { readQualityReport } from './quality'

export function notifyDocumentResult(summary: string | null, successMessage: string): void {
  const report = readQualityReport(summary)
  if (report.checked && report.warnings.length) {
    ElMessage.warning({ message: t('已入库，请检查解析质量。可在文档列表或切分页面查看详情。', 'Document indexed. Review parsing warnings in Documents or Chunks.'), duration: 6000 })
  } else {
    ElMessage.success(successMessage)
  }
}
