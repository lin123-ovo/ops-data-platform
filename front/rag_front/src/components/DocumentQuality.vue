<script setup lang="ts">
import { t } from '@/utils/locale'
import { computed } from 'vue'
import { readQualityReport } from '@/utils/quality'

const props = defineProps<{ summary?: string | null; compact?: boolean }>()
const report = computed(() => readQualityReport(props.summary))
const hasWarnings = computed(() => report.value.checked && report.value.warnings.length > 0)
const label = computed(() => !report.value.checked ? t('未检测', 'Not checked') : hasWarnings.value ? t('需检查', 'Needs review') : t('未发现所检异常', 'No issues found'))
</script>

<template>
  <template v-if="compact">
    <el-popover v-if="hasWarnings" placement="top" :width="360" trigger="click">
      <template #reference>
        <el-button size="small" type="warning" plain>{{ t('需检查 · 查看详情', 'Needs review · View details') }}</el-button>
      </template>
      <p>{{ t('最近一次入库或重建的解析质量提示：', 'Parsing notes from the latest import or index rebuild.') }}</p>
      <ul><li v-for="warning in report.warnings" :key="warning">{{ warning }}</li></ul>
    </el-popover>
    <el-tag v-else :type="report.checked ? 'success' : 'info'">{{ label }}</el-tag>
  </template>
  <el-alert
    v-else
    :title="`${t('解析质量', 'Parse quality')} · ${label}`"
    :type="hasWarnings ? 'warning' : 'info'"
    :closable="false"
    show-icon
  >
    <template v-if="hasWarnings">
      <ul><li v-for="warning in report.warnings" :key="warning">{{ warning }}</li></ul>
    </template>
    <p v-if="report.checked">{{ t('结果来自最近一次入库或索引重建；片段编辑不会重新检查整篇文档。', 'Results reflect the latest import or index rebuild. Editing a chunk does not rerun the document checks.') }}</p>
    <p v-else>{{ t('该文档暂无检测结果。下次入库或重建索引时会进行检查。', 'The next import or index rebuild will check this document.') }}</p>
  </el-alert>
</template>
