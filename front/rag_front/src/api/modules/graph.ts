import { http } from '@/api/http'
import type { GraphJob, GraphState, GraphSubgraph } from '@/types/api'
export async function buildGraph(document_ids: string[]) {
  const { data } = await http.post<{ items: GraphJob[] }>('/graph/build', { document_ids })
  return data
}
export async function fetchGraphTasks(params: { knowledge_base?: string; document_id?: string; offset?: number } = {}) {
  const { data } = await http.get<{ enabled: boolean; items: GraphJob[] }>('/graph/tasks', { params })
  return data
}
export async function fetchGraphStates(offset = 0) {
  const { data } = await http.get<{ enabled: boolean; items: GraphState[] }>('/graph/states', { params: { offset } })
  return data
}
export async function fetchSubgraph(params: { knowledge_base: string; document_id?: string; entity?: string }) {
  const { data } = await http.get<GraphSubgraph>('/graph/subgraph', { params })
  return data
}
