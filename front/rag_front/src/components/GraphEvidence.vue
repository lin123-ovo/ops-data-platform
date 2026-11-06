<script setup lang="ts">
import { t } from '@/utils/locale'
import type { GraphPath } from '@/types/api'
defineProps<{ paths?: GraphPath[] }>()
</script>
<template>
  <el-collapse v-if="paths?.length" class="graph-evidence">
    <el-collapse-item :title="t('关系路径与原文证据', 'Relationship paths and source evidence')" name="evidence">
      <article v-for="(path, i) in paths" :key="i" class="graph-path">
        <p><strong>{{ path.entities.map(e => e.name).join(' ↔ ') }}</strong></p>
        <p v-for="assertion in path.assertions" :key="assertion.id">
          <el-tag size="small">{{ assertion.predicate }}</el-tag> {{ assertion.evidence }}
          <router-link :to="{ name: 'chunks', query: { documentId: assertion.document_id, chunkId: assertion.chunk_id } }">{{ t('查看原文', 'View source') }}</router-link>
        </p>
      </article>
    </el-collapse-item>
  </el-collapse>
</template>
<style scoped>
.graph-path { padding: 8px 0; border-bottom: 1px solid var(--el-border-color); }
.graph-path p { white-space: pre-wrap; overflow-wrap: anywhere; }
a { margin-left: 8px; }
</style>
