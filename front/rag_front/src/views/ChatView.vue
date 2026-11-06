<script setup lang="ts">
import { demoLabel, t } from '@/utils/locale'
import { fetchOpsServices, opsEnabled } from '@/api/modules/ops'
import type { OpsService } from '@/api/modules/ops'
import { notifyError } from '@/api/http'
import OpsMetricCard from '@/components/OpsMetricCard.vue'
import { lastFullWeek } from '@/utils/opsDates'
import GraphEvidence from '@/components/GraphEvidence.vue'
import type { RetrievalMode } from '@/types/api'
import {
  ChatDotRound,
  Connection,
  Delete,
  Plus,
  Promotion,
  Refresh,
  VideoPause,
} from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onMounted, ref, watch, nextTick } from 'vue'

import { useAppStore } from '@/stores/app'
import { useChatStore } from '@/stores/chat'
import { formatDateTime, formatScore, truncateText } from '@/utils/format'

const appStore = useAppStore()
const chatStore = useChatStore()

const draftMessage = ref('')
const topK = ref(5)
const retrievalMode = ref<RetrievalMode>('hybrid')
const knowledgeBase = ref('default')
const chatMode = ref<'rag' | 'ops'>('rag')
const opsPeriod = ref(lastFullWeek())
const serviceId = ref('')
const services = ref<OpsService[]>([])

async function loadServices() {
  try {
    services.value = await fetchOpsServices()
  } catch (error) {
    notifyError(error, t('读取服务目录失败', 'Could not load the service catalog'))
  }
}

watch(chatMode, (mode) => {
  if (mode === 'ops') void loadServices()
})
const messageListRef = ref<HTMLElement | null>(null)

const sessions = computed(() => chatStore.sessions)
const activeSessionId = computed(() => chatStore.activeSessionId)
const activeTurns = computed(() => chatStore.activeTurns)
const selectedTurn = computed(() => chatStore.selectedTurn)

function scrollToBottom() {
  nextTick(() => {
    if (messageListRef.value) {
      messageListRef.value.scrollTop = messageListRef.value.scrollHeight
    }
  })
}

watch(activeTurns, () => {
  scrollToBottom()
}, { deep: true })

function summarizeSourceFilenames(sourceChunks: Array<{ filename?: string | null }>) {
  const filenames = Array.from(new Set(sourceChunks.map((item) => item.filename).filter(Boolean)))
  return filenames.join(' / ')
}

function resetComposer() {
  draftMessage.value = ''
}

async function initializePage() {
  await Promise.all([appStore.refreshHealth(true), chatStore.loadSessions()])

  if (chatStore.activeSessionId) {
    await chatStore.loadHistory(chatStore.activeSessionId)
    return
  }

  chatStore.createBlankSession()
}

async function handleCreateSession() {
  chatStore.createBlankSession()
  resetComposer()
  ElMessage.success(t('已创建新的空会话', 'New conversation started'))
}

async function handleSelectSession(sessionId: string) {
  await chatStore.selectSession(sessionId)
}

async function handleDeleteSession(sessionId: string) {
  try {
    await ElMessageBox.confirm(t('删除后会清空该会话的历史与短期记忆，是否继续？', 'Delete the history and memory for this conversation?'), t('删除会话确认', 'Delete conversation'), {
      type: 'warning',
      confirmButtonText: t('确认删除', 'Delete'),
      cancelButtonText: t('取消', 'Cancel'),
    })
    await chatStore.deleteSession(sessionId)
    if (!chatStore.activeSessionId) {
      chatStore.createBlankSession()
    }
  } catch {
    // 用户主动取消删除时不需要额外提示。
  }
}

async function handleSendMessage() {
  const message = draftMessage.value.trim()
  if (!message) {
    ElMessage.warning(t('请输入消息内容', 'Enter a message'))
    return
  }

  if (chatMode.value === 'ops' && (!opsPeriod.value.start_date || !opsPeriod.value.end_date)) {
    ElMessage.warning(t('请选择运维指标日期区间', 'Select the reporting date range'))
    return
  }
  await chatStore.sendMessage(
    message, topK.value, retrievalMode.value, knowledgeBase.value || undefined,
    chatMode.value === 'ops' ? { ...opsPeriod.value, service_id: serviceId.value || undefined } : undefined,
  )
  resetComposer()
}

function handleStopStreaming() {
  chatStore.stopStreaming()
}

onMounted(async () => {
  await initializePage()
})
</script>

<template>
  <div class="page-grid chat-page-grid">
    <section class="panel span-12 toolbar-card">
      <div>
        <div class="panel-kicker">Agent Console</div>
        <h2>{{ t('多轮对话、工具调用与溯源结果全链路观测', 'Chat, tools, and sources') }}</h2>
        <p class="page-description">
          {{ t('当前页面直连后端流式对话接口，可查看会话历史、实时生成内容、工具调用过程以及最终命中的来源 Chunk。', 'View conversation history, streaming responses, tool calls, and source excerpts.') }}
        </p>
      </div>
      <div class="toolbar-actions wrap">
        <el-select v-if="opsEnabled" v-model="chatMode" style="width:140px"
          :disabled="chatStore.sending" :aria-label="t('对话模式', 'Chat mode')">
          <el-option :label="t('文档问答', 'Document Q&A')" value="rag" />
          <el-option :label="t('运维数据', 'Operations Data')" value="ops" />
        </el-select>
        <el-select v-model="retrievalMode" style="width:170px" :disabled="chatStore.sending" :aria-label="t('检索模式', 'Retrieval mode')"><el-option :label="t('混合检索', 'Hybrid search')" value="hybrid" /><el-option :label="t('图谱增强检索', 'Graph-enhanced search')" value="graph_hybrid" /></el-select>
        <template v-if="chatMode === 'ops'">
          <el-select v-model="serviceId" style="width:150px" :disabled="chatStore.sending" :aria-label="t('运维服务', 'Service')" :empty-values="[null, undefined]">
            <el-option :label="t('全部服务', 'All services')" value="" />
            <el-option v-for="item in services" :key="item.service_id" :label="demoLabel(item.name)" :value="item.service_id" />
          </el-select>
          <el-button :disabled="chatStore.sending" @click="opsPeriod = lastFullWeek()">{{ t('上周', 'Last week') }}</el-button>
          <el-date-picker v-model="opsPeriod.start_date" :clearable="false" type="date" value-format="YYYY-MM-DD"
            :disabled="chatStore.sending" :aria-label="t('运维统计开始日期', 'Start date')" />
          <el-date-picker v-model="opsPeriod.end_date" :clearable="false" type="date" value-format="YYYY-MM-DD"
            :disabled="chatStore.sending" :aria-label="t('运维统计结束日期（不含）', 'End date (exclusive)')" />
        </template>
        <el-input v-else v-model="knowledgeBase" style="width:150px" :disabled="chatStore.sending" :placeholder="t('知识库', 'Knowledge base')" :aria-label="t('知识库', 'Knowledge base')" />
        <el-tag :type="appStore.backendReady ? 'success' : 'danger'" effect="dark">
          {{ appStore.backendReady ? t('后端可用', 'Backend online') : t('等待后端', 'Waiting for backend') }}
        </el-tag>
        <el-button :icon="Refresh" @click="initializePage">{{ t('刷新会话', 'Refresh chats') }}</el-button>
        <el-button :icon="Plus" @click="handleCreateSession">{{ t('新建会话', 'New chat') }}</el-button>
      </div>
    </section>

    <section class="panel span-3 section-card fixed-height">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Sessions</div>
          <h3>{{ t('会话列表', 'Conversations') }}</h3>
        </div>
        <el-tag effect="dark">{{ sessions.length }}</el-tag>
      </div>

      <div class="chat-session-list">
        <article
          v-for="session in sessions"
          :key="session.session_id"
          class="session-row"
          :class="{ 'is-active': activeSessionId === session.session_id }"
        >
          <div class="session-row-top">
            <strong>{{ truncateText(session.latest_question || session.session_id, 24) }}</strong>
            <el-button
              text
              type="danger"
              :icon="Delete"
              @click.stop="handleDeleteSession(session.session_id)"
            />
          </div>
          <div class="session-row-body" @click="handleSelectSession(session.session_id)">
            <div class="stack-meta">
              <span>{{ session.message_count }} {{ t('轮', 'turns') }}</span>
              <span>{{ formatDateTime(session.updated_at) }}</span>
            </div>
            <p class="stack-text">{{ truncateText(session.latest_answer || t('暂无回答', 'No response yet'), 40) }}</p>
          </div>
        </article>
      </div>

      <el-empty
        v-if="!sessions.length"
        :description="t('暂无历史会话，可直接新建或发送首条消息', 'Start a chat or send your first message')"
      />
    </section>

    <section class="panel span-6 section-card fixed-height">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Conversation</div>
          <h3>{{ t('对话窗口', 'Chat') }}</h3>
        </div>
        <el-tag effect="dark">{{ activeTurns.length }} {{ t('条记录', 'messages') }}</el-tag>
      </div>

      <div class="chat-message-list" ref="messageListRef">
        <template v-for="turn in activeTurns" :key="turn.id">
          <!-- 用户消息气泡 -->
          <div class="chat-bubble-wrapper is-user">
            <div class="chat-bubble">
              <div class="bubble-header">
                <span class="bubble-time">{{ formatDateTime(turn.createdAt) }}</span>
                <span class="bubble-name">{{ t('我', 'You') }}</span>
              </div>
              <div class="bubble-content">{{ turn.userQuestion }}</div>
            </div>
          </div>

          <!-- Agent 回复气泡 -->
          <div
            class="chat-bubble-wrapper is-agent"
            :class="{ 'is-selected': chatStore.selectedTurnId === turn.id }"
            @click="chatStore.selectedTurnId = turn.id"
          >
            <div class="chat-bubble">
              <div class="bubble-header">
                <span class="bubble-name">Agent</span>
                <span class="bubble-time">{{ turn.latencyMs ? `${turn.latencyMs} ms` : turn.pending ? t('生成中', 'Generating') : '--' }}</span>
              </div>
              <OpsMetricCard v-if="turn.metricSnapshot" :metric="turn.metricSnapshot" :link-to-details="opsEnabled" :disable-details="!opsEnabled" />
              <div class="bubble-content answer-text">
                <template v-if="turn.answer">{{ turn.answer }}</template>
                <template v-else-if="turn.pending">
                  <div class="typing-indicator">
                    <span></span><span></span><span></span>
                  </div>
                </template>
                <template v-else>{{ t('暂无回答', 'No response yet') }}</template>
              </div>

              <!-- 执行状态与来源标识 -->
              <div class="bubble-footer">
                <div class="pill-row">
                  <span class="meta-pill">route: {{ turn.route }}</span>
                  <span class="meta-pill" v-if="turn.sourceChunks.length">
                    sources: {{ turn.sourceChunks.length }}
                  </span>
                  <span class="meta-pill" :class="{ 'is-danger': turn.failed }">
                    {{ turn.failed ? turn.errorMessage || t('执行失败', 'Failed') : turn.pending ? t('执行中', 'In progress') : t('已完成', 'Complete') }}
                  </span>
                </div>
                <div v-if="turn.sourceChunks.length" class="stack-meta source-summary">
                  <span>{{ t('检索命中文档', 'Retrieved documents') }} · {{ summarizeSourceFilenames(turn.sourceChunks) || t('未命名文档', 'Untitled document') }}</span>
                </div>
              </div>
            </div>
          </div>
        </template>
      </div>

      <div class="chat-composer">
        <el-input
          v-model="draftMessage"
          type="textarea"
          :rows="4"
          resize="none"
          :placeholder="t('输入你的问题，例如：总结 rule.md 的编码规范，并给出来源 chunk', 'Ask a question and request sources')"
          @keyup.ctrl.enter="handleSendMessage"
        />
        <div class="composer-actions">
          <div class="composer-config">
            <span>Top K</span>
            <el-input-number v-model="topK" :min="1" :max="20" />
          </div>
          <div class="toolbar-actions">
            <el-button
              :icon="VideoPause"
              :disabled="!chatStore.sending"
              @click="handleStopStreaming"
            >
              {{ t('停止生成', 'Stop') }}
            </el-button>
            <el-button
              type="primary"
              :icon="Promotion"
              :loading="chatStore.sending"
              @click="handleSendMessage"
            >
              {{ t('发送消息', 'Send message') }}
            </el-button>
          </div>
        </div>
      </div>
    </section>

    <section class="panel span-3 section-card fixed-height">
      <div class="section-heading">
        <div>
          <div class="panel-kicker">Trace</div>
          <h3>{{ t('执行轨迹与来源', 'Activity and sources') }}</h3>
        </div>
        <el-tag effect="dark">{{ selectedTurn?.events.length || 0 }} {{ t('个事件', 'events') }}</el-tag>
      </div>

      <template v-if="selectedTurn">
        <div class="detail-grid compact">
          <div class="detail-item"><span>Session</span><strong>{{ selectedTurn.sessionId }}</strong></div>
          <div class="detail-item"><span>Route</span><strong>{{ selectedTurn.route }}</strong></div>
          <div class="detail-item"><span>Latency</span><strong>{{ selectedTurn.latencyMs ?? '--' }}</strong></div>
          <div class="detail-item"><span>Source Chunks</span><strong>{{ selectedTurn.sourceChunks.length }}</strong></div>
        </div>

        <div class="trace-section">
          <div class="code-panel-title">{{ t('工具事件', 'Tool activity') }}</div>
          <div class="timeline-list">
            <article v-for="event in selectedTurn.events" :key="`${selectedTurn.id}-${event.timestamp}-${event.type}`" class="timeline-item">
              <div class="timeline-header">
                <strong>{{ event.type }}</strong>
                <span>{{ formatDateTime(event.timestamp) }}</span>
              </div>
              <div class="stack-meta">
                <span v-if="event.phase">phase: {{ event.phase }}</span>
                <span v-if="event.tool_name">tool: {{ event.tool_name }}</span>
                <span v-if="event.status">status: {{ event.status }}</span>
              </div>
              <pre v-if="event.args">{{ JSON.stringify(event.args, null, 2) }}</pre>
              <p v-else class="stack-text">{{ event.content || event.message || t('无额外内容', 'No additional details') }}</p>
            </article>
          </div>
        </div>

        <div class="trace-section">
          <div class="code-panel-title">{{ t('来源 Chunk', 'Source excerpts') }}</div>
          <div v-if="selectedTurn.sourceChunks.length" class="source-list">
            <article v-for="item in selectedTurn.sourceChunks" :key="`${selectedTurn.id}-${item.ref_id}`" class="source-card">
              <div class="result-card-top">
                <div>
                  <div class="stack-title">{{ item.filename || 'unknown' }}</div>
                  <div class="stack-meta">
                    <span>ref #{{ item.ref_id }}</span>
                    <span>chunk #{{ item.chunk_index ?? '--' }}</span>
                    <span>page {{ item.page_number ?? '--' }}</span>
                  </div>
                </div>
                <el-tag type="success">score {{ formatScore(item.score) }}</el-tag>
              </div>
              <GraphEvidence :paths="item.graph_paths" /><p class="stack-text">{{ item.content }}</p>
              <div class="pill-row">
                <span class="meta-pill">splitter: {{ item.splitter_name || '--' }}</span>
                <span class="meta-pill">parser: {{ item.parser_name || '--' }}</span>
                <span class="meta-pill">section: {{ item.section_title || item.section_type || '--' }}</span>
              </div>
            </article>
          </div>
          <el-empty v-else :description="t('当前轮次还没有来源片段', 'No sources for this response')" />
          <el-alert v-if="selectedTurn?.degradation_reason" :title="selectedTurn.degradation_reason" type="warning" :closable="false" />
        </div>
      </template>

      <el-empty v-else :description="t('选择一轮对话后查看执行轨迹和来源 Chunk', 'Select a response to view activity and sources')" />
    </section>
  </div>
</template>
