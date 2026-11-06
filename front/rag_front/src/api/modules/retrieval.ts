import type { RetrievalSearchResponse } from '@/types/api'
import { http } from '@/api/http'

export async function searchRetrieval(query: string, top_k: number, retrieval_mode: 'hybrid' | 'graph_hybrid' = 'hybrid', knowledge_base?: string): Promise<RetrievalSearchResponse> {
  const { data } = await http.post<RetrievalSearchResponse>('/retrieval/search', {
    query,
    top_k, retrieval_mode, knowledge_base,
  })
  return data
}
