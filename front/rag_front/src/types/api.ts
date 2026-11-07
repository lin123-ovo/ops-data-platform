import type { MetricSnapshot } from '@/api/modules/ops'

export interface ServiceHealthItem {
  ok: boolean
  error: string | null
}

export interface HealthResponse {
  ok: boolean
  services: Record<string, ServiceHealthItem>
}

export interface SplitterOptionItem {
  name: string
  description: string
}

export interface DocumentItem {
  id: string
  knowledge_base: string
  filename: string
  file_type: string
  source_path: string | null
  file_size: number | null
  status: string
  chunk_count: number
  summary: string | null
  created_at: string
  updated_at: string
}

export interface DocumentIngestResponse {
  document: DocumentItem
  message: string
}

export interface ChunkItem {
  id: string
  document_id: string
  chunk_index: number
  content: string
  metadata_json: Record<string, unknown>
  token_count: number
  page_number: number | null
  start_offset: number | null
  end_offset: number | null
  vector_id: string | null
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface RetrievalHitItem {
  rank_graph?: number | null
  graph_score?: number | null
  graph_entities?: GraphEntity[]
  graph_paths?: GraphPath[]
  chunk_id: string | null
  document_id: string | null
  filename: string | null
  file_type: string | null
  chunk_index: number | null
  content: string
  score: number
  vector_score: number | null
  bm25_score: number | null
  fused_score: number | null
  retrieval_source: string | null
  retrieval_sources: string[]
  rank_vector: number | null
  rank_bm25: number | null
  rank_fused: number | null
  splitter_name: string | null
  parser_name: string | null
  section_type: string | null
  section_title: string | null
  page_number: number | null
  source_path: string | null
  start_offset: number | null
  end_offset: number | null
}

export interface RetrievalSearchResponse extends GraphDiagnostics {
  items: RetrievalHitItem[]
}

export interface SourceChunkItem extends RetrievalHitItem {
  ref_id: number
}

export interface WebSourceItem {
  ref_id: string
  title: string
  url: string
  content: string
  retrieved_at: string
  published_date: string | null
}

export type WebSearchStatus = 'not_requested' | 'unavailable' | 'ok' | 'no_results' | 'error'

export interface WebSearchFields {
  web_sources?: WebSourceItem[]
  web_search_status?: WebSearchStatus
  web_search_error?: string | null
  web_search_summary?: string | null
}

export interface ChatResponse extends GraphDiagnostics, WebSearchFields {
  metric_snapshot?: MetricSnapshot | null
  session_id: string
  answer: string
  route: string
  latency_ms: number
  source_chunks: SourceChunkItem[]
  created_at: string
}

export interface ChatHistoryItem extends GraphDiagnostics, WebSearchFields {
  metric_snapshot?: MetricSnapshot | null
  id: string
  session_id: string | null
  user_question: string
  answer: string | null
  route: string
  latency_ms: number | null
  source_chunks: SourceChunkItem[]
  created_at: string
  updated_at: string
}

export interface SessionSummaryItem {
  session_id: string
  latest_question: string
  latest_answer: string | null
  message_count: number
  updated_at: string
}

export interface SessionClearResponse {
  session_id: string
  deleted_query_log_count: number
  cleared_memory: boolean
}

export type ChatStreamEventName =
  | 'status'
  | 'token'
  | 'tool_call'
  | 'tool_result'
  | 'tool_error'
  | 'sources'
  | 'web_sources'
  | 'metric_snapshot'
  | 'error'
  | 'done'

export interface ChatToolEvent {
  type: 'tool_call' | 'tool_result' | 'tool_error' | 'status' | 'error'
  step?: string
  tool_name?: string
  tool_call_id?: string
  status?: string
  content?: string
  message?: string
  args?: Record<string, unknown>
  session_id?: string
  phase?: string
  timestamp: string
}

export interface ChatTurn extends GraphDiagnostics {
  webSources: WebSourceItem[]
  webSearchStatus: WebSearchStatus
  webSearchError: string | null
  webSearchSummary: string | null
  metricSnapshot?: MetricSnapshot | null
  id: string
  sessionId: string
  userQuestion: string
  answer: string
  route: string
  latencyMs: number | null
  sourceChunks: SourceChunkItem[]
  createdAt: string
  updatedAt: string
  pending?: boolean
  failed?: boolean
  errorMessage?: string
  events: ChatToolEvent[]
}

export type RetrievalMode = 'hybrid' | 'graph_hybrid'
export interface GraphEntity { id: string; name: string; type: string; qualifier?: string; aliases?: string[] }
export interface GraphAssertion {
  id: string; predicate: string; evidence: string; chunk_id: string; document_id: string
  subject_entity_id: string; object_entity_id: string; subject_id?: string; object_id?: string
  version: string; content_hash: string; token: string
}
export interface GraphPath { entities: GraphEntity[]; assertions: GraphAssertion[] }
export interface GraphDiagnostics { graph_status?: string; degradation_reason?: string | null }
export interface GraphJob {
  id: string; document_id: string; knowledge_base: string; status: string
  progress: number; total: number; attempts: number; error: string | null
  model: string; schema_version: string; metrics: Record<string, unknown>; updated_at: string
}
export interface GraphState { document_id: string; status: string; desired_version: string; published_version: string | null }
export interface GraphSubgraph extends GraphDiagnostics { nodes: GraphEntity[]; edges: GraphAssertion[] }
