import { createRouter, createWebHistory } from 'vue-router'

import { opsEnabled } from '@/api/modules/ops'

const router = createRouter({
  history: createWebHistory(),
  scrollBehavior() {
    return { top: 0 }
  },
  // AppShell 是侧栏/顶栏外壳，children 是菜单对应的六个业务页面。
  // 截图 URL /dashboard 对应 views/DashboardView.vue。
  routes: [
    {
      path: '/',
      component: () => import('@/layouts/AppShell.vue'),
      redirect: '/dashboard',
      children: [
        ...(opsEnabled ? [{
          path: '/ops', name: 'ops', component: () => import('@/views/OpsView.vue'), meta: { title: ['运维数据', 'Operations'] },
        }] : []),
        { path: '/graph', name: 'graph', component: () => import('@/views/GraphView.vue'), meta: { title: ['知识图谱', 'Knowledge Graph'] } },
        {
          path: '/dashboard',
          name: 'dashboard',
          component: () => import('@/views/DashboardView.vue'),
          meta: { title: ['总览看板', 'Dashboard'] },
        },
        {
          path: '/ingest',
          name: 'ingest',
          component: () => import('@/views/IngestView.vue'),
          meta: { title: ['上传入库', 'Import Documents'] },
        },
        {
          path: '/documents',
          name: 'documents',
          component: () => import('@/views/DocumentsView.vue'),
          meta: { title: ['文档管理', 'Documents'] },
        },
        {
          path: '/chunks',
          name: 'chunks',
          component: () => import('@/views/ChunksView.vue'),
          meta: { title: ['Chunk 管理', 'Chunks'] },
        },
        {
          path: '/retrieval',
          name: 'retrieval',
          component: () => import('@/views/RetrievalView.vue'),
          meta: { title: ['检索调试', 'Retrieval Console'] },
        },
        {
          path: '/chat',
          name: 'chat',
          component: () => import('@/views/ChatView.vue'),
          meta: { title: ['Agent 对话', 'Agent Chat'] },
        },
      ],
    },
  ],
})

export default router
