from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field
from app.ops.metrics import MetricSnapshot, ServiceScope
from app.schemas.graph import RetrievalOptions, GraphFields, GraphDiagnostics


class ChatRequest(RetrievalOptions):
    """聊天请求体。"""

    session_id: str = Field(description='会话 ID，用于多轮记忆的 thread_id')
    message: str = Field(description='用户问题')
    top_k: int = Field(default=5, ge=1, le=8, description='知识库检索结果数量上限')
    mode: Literal['rag', 'ops'] = 'rag'
    reply_language: Literal['en'] | None = None
    start_date: date | None = None
    end_date: date | None = None
    service_id: ServiceScope | None = None


class SourceChunkItem(GraphFields):
    """答案溯源条目。"""

    ref_id: int
    chunk_id: str | None = None
    document_id: str | None = None
    filename: str | None = None
    file_type: str | None = None
    chunk_index: int | None = None
    content: str
    score: float
    vector_score: float | None = None
    bm25_score: float | None = None
    fused_score: float | None = None
    retrieval_source: str | None = None
    retrieval_sources: list[str] = Field(default_factory=list)
    rank_vector: int | None = None
    rank_bm25: int | None = None
    rank_fused: int | None = None
    splitter_name: str | None = None
    parser_name: str | None = None
    section_type: str | None = None
    section_title: str | None = None
    page_number: int | None = None
    source_path: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None


class WebSourceItem(BaseModel):
    ref_id: str
    title: str
    url: str
    content: str
    retrieved_at: datetime
    published_date: str | None = None


class WebSearchFields(BaseModel):
    web_sources: list[WebSourceItem] = Field(default_factory=list)
    web_search_status: Literal['not_requested', 'unavailable', 'ok', 'no_results', 'error'] = 'not_requested'
    web_search_error: str | None = None
    web_search_summary: str | None = None


class ChatResponse(GraphDiagnostics, WebSearchFields):
    """同步聊天响应。"""

    session_id: str
    answer: str
    route: str = 'agent_rag'
    latency_ms: int
    metric_snapshot: MetricSnapshot | None = None
    source_chunks: list[SourceChunkItem] = Field(default_factory=list)
    created_at: datetime


class ChatHistoryItem(GraphDiagnostics, WebSearchFields):
    """单轮会话历史记录。"""

    id: str
    session_id: str | None = None
    user_question: str
    answer: str | None = None
    route: str
    latency_ms: int | None = None
    metric_snapshot: MetricSnapshot | None = None
    source_chunks: list[SourceChunkItem] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class SessionSummaryItem(BaseModel):
    """会话摘要列表项。"""

    session_id: str
    latest_question: str
    latest_answer: str | None = None
    message_count: int
    updated_at: datetime


class SessionClearResponse(BaseModel):
    """清空会话后的响应。"""

    session_id: str
    deleted_query_log_count: int
    cleared_memory: bool
