<script setup lang="ts">
import {
  ChatDotRound,
  Connection,
  DataLine,
  Document,
  Expand,
  HomeFilled,
  Refresh,
  Upload,
} from '@element-plus/icons-vue'
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import brandIcon from '@/assets/ops-platform.svg'
import { language, setLanguage, t } from '@/utils/locale'
import { opsEnabled } from '@/api/modules/ops'
import { useAppStore } from '@/stores/app'
import { useDocumentStore } from '@/stores/documents'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const documentStore = useDocumentStore()
const dashboard = ref<{ refreshOverview?: () => Promise<void> } | null>(null)

const menuItems = computed(() => [
  { label: t('总览看板', 'Dashboard'), routeName: 'dashboard', icon: HomeFilled },
  ...(opsEnabled ? [{ label: t('运维数据', 'Operations'), routeName: 'ops', icon: DataLine }] : []),
  { label: t('知识图谱', 'Knowledge Graph'), routeName: 'graph', icon: DataLine },
  { label: t('上传入库', 'Import Documents'), routeName: 'ingest', icon: Upload },
  { label: t('文档管理', 'Documents'), routeName: 'documents', icon: Document },
  { label: t('Chunk 管理', 'Chunks'), routeName: 'chunks', icon: Expand },
  { label: t('检索调试', 'Retrieval Console'), routeName: 'retrieval', icon: DataLine },
  { label: t('Agent 对话', 'Agent Chat'), routeName: 'chat', icon: ChatDotRound },
])

const activeMenu = computed(() => String(route.name || 'dashboard'))
const pageTitle = computed(() => {
  const title = route.meta.title
  return Array.isArray(title) ? t(title[0], title[1]) : 'Ops Data Platform'
})
const backendStatusType = computed(() => (appStore.backendReady ? 'success' : 'danger'))
const backendStatusLabel = computed(() => (appStore.backendReady ? t('后端已连接', 'Backend connected') : t('后端待连接', 'Waiting for backend')))

function goTo(routeName: string) {
  router.push({ name: routeName })
}

async function refreshWorkspace() {
  await Promise.all([appStore.refreshHealth(true), documentStore.initialize()])
}

async function refreshAll() {
  await Promise.all([refreshWorkspace(), dashboard.value?.refreshOverview?.()])
}

onMounted(refreshWorkspace)
</script>

<template>
  <div class="app-shell">
    <aside class="app-sidebar">
      <div class="brand-card">
        <img class="brand-mark" :src="brandIcon" alt="" width="52" height="52">
        <div>
          <div class="brand-title">Ops Data Platform</div>
          <div class="brand-subtitle">{{ opsEnabled ? t('知识检索与运维分析', 'Knowledge retrieval and SLA analytics') : t('知识检索与 Agent 问答', 'Knowledge retrieval and agent chat') }}</div>
        </div>
      </div>

      <nav class="nav-list">
        <button
          v-for="item in menuItems"
          :key="item.routeName"
          class="nav-item"
          :class="{ 'is-active': activeMenu === item.routeName }"
          @click="goTo(item.routeName)"
        >
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </button>
      </nav>

      <div class="sidebar-footer panel">
        <div class="status-row">
          <div>
            <div class="panel-kicker">{{ t('链路状态', 'System status') }}</div>
            <div class="panel-title">{{ backendStatusLabel }}</div>
          </div>
          <el-tag :type="backendStatusType" effect="dark">
            {{ appStore.health?.ok ? 'READY' : 'CHECK' }}
          </el-tag>
        </div>
        <div v-if="appStore.health" class="service-grid">
          <div v-for="(service, key) in appStore.health.services" :key="key" class="service-pill">
            <el-icon><Connection /></el-icon>
            <span>{{ key }}</span>
            <i :class="['service-dot', service.ok ? 'is-ok' : 'is-bad']" />
          </div>
        </div>
      </div>
    </aside>

    <div class="app-main">
      <header class="topbar panel">
        <div>
          <div class="panel-kicker">Workspace</div>
          <h1 class="topbar-title">{{ pageTitle }}</h1>
        </div>
        <div class="topbar-actions">
          <el-button :aria-label="t('切换到英文', 'Switch to Chinese')"
            @click="setLanguage(language === 'en' ? 'zh' : 'en')">
            {{ language === 'en' ? '中文' : 'English' }}
          </el-button>
          <el-button :icon="Refresh" @click="refreshAll">{{ t('刷新数据', 'Refresh data') }}</el-button>
        </div>
      </header>

      <main class="content-area">
        <RouterView v-slot="{ Component }">
          <component :is="Component" v-if="route.name === 'dashboard'" ref="dashboard" />
          <component :is="Component" v-else />
        </RouterView>
      </main>
    </div>
  </div>
</template>
