import { defineStore } from 'pinia'
import { reactive } from 'vue'
import { language, t } from '@/utils/locale'
import type { MetricSnapshot, OpsQuery } from '@/api/modules/ops'

import { clearSession, fetchSessionHistory, fetchSessions, streamChat } from '@/api/modules/chat'
import { notifyError } from '@/api/http'
import type {
  RetrievalMode,
  ChatHistoryItem,
  ChatStreamEventName,
  ChatToolEvent,
  ChatTurn,
  SessionSummaryItem,
  SourceChunkItem,
  WebSearchFields,
} from '@/types/api'
import { createSessionId } from '@/utils/format'

function historyItemToTurn(item: ChatHistoryItem): ChatTurn {
  return {
    id: item.id,
    sessionId: item.session_id || '',
    userQuestion: item.user_question,
    answer: item.answer || '',
    route: item.route,
    latencyMs: item.latency_ms,
    sourceChunks: item.source_chunks,
    webSources: item.web_sources || [],
    webSearchStatus: item.web_search_status || 'not_requested',
    webSearchError: item.web_search_error || null,
    webSearchSummary: item.web_search_summary || null,
    metricSnapshot: item.metric_snapshot,
    createdAt: item.created_at,
    updatedAt: item.updated_at,
    graph_status: item.graph_status, degradation_reason: item.degradation_reason,
    events: [],
  }
}

function updateWebSources(turn: ChatTurn, payload: WebSearchFields) {
  turn.webSources = payload.web_sources || []
  turn.webSearchStatus = payload.web_search_status || 'not_requested'
  turn.webSearchError = payload.web_search_error || null
  turn.webSearchSummary = payload.web_search_summary || null
}

function buildEvent(
  type: ChatToolEvent['type'],
  payload: Partial<ChatToolEvent> = {},
): ChatToolEvent {
  return {
    type,
    timestamp: new Date().toISOString(),
    ...payload,
  }
}

export const useChatStore = defineStore('chat', {
  state: () => ({
    sessions: [] as SessionSummaryItem[],
    sessionTurns: {} as Record<string, ChatTurn[]>,
    activeSessionId: '',
    selectedTurnId: '',
    loadingSessions: false,
    loadingHistory: false,
    sending: false,
    currentAbortController: null as AbortController | null,
  }),
  getters: {
    activeTurns(state): ChatTurn[] {
      return state.sessionTurns[state.activeSessionId] || []
    },
    selectedTurn(): ChatTurn | null {
      const turns = this.activeTurns
      return turns.find((item) => item.id === this.selectedTurnId) || turns.at(-1) || null
    },
  },
  actions: {
    ensureActiveSession() {
      if (!this.activeSessionId) {
        this.activeSessionId = createSessionId()
      }
      if (!this.sessionTurns[this.activeSessionId]) {
        this.sessionTurns[this.activeSessionId] = []
      }
      return this.activeSessionId
    },
    async loadSessions() {
      this.loadingSessions = true
      try {
        this.sessions = await fetchSessions()
        const firstSession = this.sessions[0]
        if (!this.activeSessionId && firstSession) {
          this.activeSessionId = firstSession.session_id
        }
      } catch (error) {
        notifyError(error, t('加载会话列表失败', 'Could not load conversations'))
      } finally {
        this.loadingSessions = false
      }
    },
    async selectSession(sessionId: string) {
      this.activeSessionId = sessionId
      await this.loadHistory(sessionId)
    },
    async loadHistory(sessionId: string) {
      this.loadingHistory = true
      try {
        const history = await fetchSessionHistory(sessionId)
        const turns = history.map(historyItemToTurn)
        this.sessionTurns[sessionId] = turns
        this.selectedTurnId = turns.at(-1)?.id || ''
      } catch (error) {
        notifyError(error, t('加载会话历史失败', 'Could not load conversation history'))
      } finally {
        this.loadingHistory = false
      }
    },
    createBlankSession() {
      this.activeSessionId = createSessionId()
      this.sessionTurns[this.activeSessionId] = []
      this.selectedTurnId = ''
    },
    stopStreaming() {
      this.currentAbortController?.abort()
      this.currentAbortController = null
      this.sending = false
    },
    async deleteSession(sessionId: string) {
      try {
        await clearSession(sessionId)
        this.sessions = this.sessions.filter((item) => item.session_id !== sessionId)
        delete this.sessionTurns[sessionId]
        if (this.activeSessionId === sessionId) {
          const nextSessionId = this.sessions[0]?.session_id || ''
          this.activeSessionId = nextSessionId
          this.selectedTurnId = ''
          if (nextSessionId) {
            await this.loadHistory(nextSessionId)
          }
        }
      } catch (error) {
        notifyError(error, t('清空会话失败', 'Could not clear the conversation'))
      }
    },
    // 页面发消息的核心：建立本轮状态 → 读取 SSE → 更新文字、来源、工具记录。
    async sendMessage(
      message: string, topK: number, retrievalMode: RetrievalMode = 'hybrid',
      knowledgeBase?: string, opsPeriod?: OpsQuery,
    ) {
      const sessionId = this.ensureActiveSession()
      const turns = this.sessionTurns[sessionId] || (this.sessionTurns[sessionId] = [])
      const turnId = `turn-${Date.now()}`
      const nextTurn = reactive<ChatTurn>({
        id: turnId,
        sessionId,
        userQuestion: message,
        answer: '',
        route: opsPeriod ? 'agent_ops' : 'agent_chat',
        latencyMs: null,
        sourceChunks: [],
        webSources: [],
        webSearchStatus: 'not_requested',
        webSearchError: null,
        webSearchSummary: null,
        createdAt: new Date().toISOString(),
        updatedAt: new Date().toISOString(),
        pending: true,
        events: [buildEvent('status', { phase: 'queued' })],
      })

      turns.push(nextTurn)
      this.selectedTurnId = turnId
      this.sending = true
      const controller = new AbortController()
      this.currentAbortController = controller

      const pushEvent = (type: ChatToolEvent['type'], payload: Partial<ChatToolEvent>) => {
        nextTurn.events.push(buildEvent(type, payload))
      }

      try {
        await streamChat(
          {
            session_id: sessionId,
            message,
            top_k: topK, retrieval_mode: retrievalMode, knowledge_base: knowledgeBase,
            reply_language: language.value === 'en' ? 'en' : undefined,
            ...(opsPeriod ? { mode: 'ops', ...opsPeriod } : {}),
          },
          {
            signal: controller.signal,
            onEvent: (streamEvent) => {
              const eventName = streamEvent.event as ChatStreamEventName
              const payload = streamEvent.data as Record<string, unknown>

              if (eventName === 'status') {
                pushEvent('status', {
                  phase: String(payload.phase || 'running'),
                  session_id: String(payload.session_id || sessionId),
                })
                return
              }

              // token 增量拼成页面上的答案；sources 单独更新来源卡片；done 标记本轮完成。
              if (eventName === 'token') {
                nextTurn.answer += String(payload.text || '')
                nextTurn.updatedAt = new Date().toISOString()
                return
              }

              if (eventName === 'tool_call') {
                pushEvent('tool_call', {
                  step: String(payload.step || ''),
                  tool_name: String(payload.tool_name || ''),
                  tool_call_id: String(payload.tool_call_id || ''),
                  args: (payload.args as Record<string, unknown>) || {},
                })
                return
              }

              if (eventName === 'tool_result' || eventName === 'tool_error') {
                pushEvent(eventName, {
                  step: String(payload.step || ''),
                  tool_name: String(payload.tool_name || ''),
                  tool_call_id: String(payload.tool_call_id || ''),
                  status: String(payload.status || ''),
                  content: String(payload.content || ''),
                })
                return
              }

              if (eventName === 'metric_snapshot') {
                nextTurn.metricSnapshot = streamEvent.data as MetricSnapshot
                return
              }

              if (eventName === 'sources') {
                nextTurn.graph_status = String(payload.graph_status || '')
                nextTurn.degradation_reason = payload.degradation_reason ? String(payload.degradation_reason) : null
                nextTurn.sourceChunks = ((payload.items as SourceChunkItem[]) || []).map((item) => ({
                  ...item,
                }))
                return
              }

              if (eventName === 'web_sources') {
                updateWebSources(nextTurn, payload as WebSearchFields)
                return
              }

              if (eventName === 'error') {
                nextTurn.failed = true
                nextTurn.pending = false
                nextTurn.errorMessage = String(payload.message || t('对话失败', 'Chat failed'))
                pushEvent('error', {
                  message: nextTurn.errorMessage,
                })
                return
              }

              if (eventName === 'done') {
                nextTurn.pending = false
                nextTurn.route = String(payload.route || 'agent_chat')
                if (typeof payload.answer === 'string') nextTurn.answer = payload.answer
                if (Array.isArray(payload.source_chunks)) nextTurn.sourceChunks = payload.source_chunks as SourceChunkItem[]
                if ('web_sources' in payload) updateWebSources(nextTurn, payload as WebSearchFields)
                nextTurn.latencyMs = Number(payload.latency_ms || 0)
                nextTurn.updatedAt = String(payload.created_at || new Date().toISOString())
              }
            },
          },
        )

        await this.loadSessions()
      } catch (error) {
        if (controller.signal.aborted) {
          nextTurn.pending = false
          nextTurn.failed = true
          nextTurn.errorMessage = t('已手动停止当前对话', 'Response stopped')
          pushEvent('error', { message: nextTurn.errorMessage })
        } else {
          const messageText = notifyError(error, t('发送消息失败', 'Could not send the message'))
          nextTurn.pending = false
          nextTurn.failed = true
          nextTurn.errorMessage = messageText
          pushEvent('error', { message: messageText })
        }
      } finally {
        this.currentAbortController = null
        this.sending = false
      }
    },
  },
})
