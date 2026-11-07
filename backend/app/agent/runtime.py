from __future__ import annotations

import logging
from concurrent.futures import Future
from threading import RLock
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar, Token, copy_context
from dataclasses import dataclass, field
from typing import Generator

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetrievalTrace:
    """记录一次 Agent 调用过程中产生的检索溯源信息。"""

    top_k: int = 5
    retrieval_mode: str = 'hybrid'
    knowledge_base: str | None = None
    reply_language: str | None = None
    diagnostics: dict = field(default_factory=dict)
    source_chunks: list[dict] = field(default_factory=list)
    knowledge_base_called: bool = False
    web_called: bool = False
    web_sources: list[dict] = field(default_factory=list)
    web_search_summaries: list[str] = field(default_factory=list)
    web_search_status: str = 'not_requested'
    web_search_error: str | None = None
    citation_corrections: int = 0
    web_queries: dict[tuple, Future] = field(default_factory=dict, repr=False)
    _lock: AbstractContextManager = field(default_factory=RLock, repr=False)

    def register(self, hits: list[dict]) -> list[dict]:
        """Stable per-turn citation IDs, including across repeated tool calls."""
        with self._lock:
            by_id = {h['chunk_id']: h for h in self.source_chunks}
            selected = []
            for hit in hits:
                key = hit.get('chunk_id')
                if not key:
                    continue
                if key not in by_id:
                    if len(self.source_chunks) >= 40:
                        continue
                    stored = {**hit, 'ref_id': len(self.source_chunks) + 1}
                    self.source_chunks.append(stored)
                    by_id[key] = stored
                else:
                    # Retain richer graph provenance without changing the citation number.
                    for field_name in ('graph_paths', 'graph_entities', 'rank_graph', 'graph_score'):
                        if hit.get(field_name):
                            by_id[key][field_name] = hit[field_name]
                selected.append(by_id[key])
            return selected

    def record_diagnostics(self, diagnostics: dict) -> None:
        """Keep a degradation visible even if a later parallel tool succeeds."""
        priorities = {'not_requested': 0, 'no_hits': 1, 'ok': 2, 'disabled': 3, 'degraded': 4}
        with self._lock:
            if priorities.get(diagnostics.get('graph_status'), 0) >= priorities.get(self.diagnostics.get('graph_status'), 0):
                self.diagnostics.update(diagnostics)

    def web_snapshot(self) -> dict:
        """Return an independent, JSON-compatible snapshot for responses and history."""
        with self._lock:
            return {
                'web_sources': [dict(source) for source in self.web_sources],
                'web_search_status': self.web_search_status,
                'web_search_error': self.web_search_error,
                'web_search_summary': '\n\n'.join(self.web_search_summaries) or None,
            }

    def route(self) -> str:
        if self.knowledge_base_called and self.web_called:
            return 'agent_mixed'
        if self.web_called:
            return 'agent_web'
        return 'agent_rag' if self.knowledge_base_called else 'agent_chat'


_current_retrieval_trace: ContextVar[RetrievalTrace | None] = ContextVar(
    'current_retrieval_trace',
    default=None,
)


@contextmanager
def bind_retrieval_trace(trace: RetrievalTrace) -> Generator[RetrievalTrace, None, None]:
    """把当前请求的检索溯源容器绑定到上下文。

    之所以使用 `contextvars` 而不是全局变量，是为了保证：
    - 多请求并发时互不串数据；
    - Agent 工具函数无需感知 FastAPI 请求对象；
    - 以后切到异步工具或更多工具时仍然能复用同一机制。
    """

    token: Token[RetrievalTrace | None] = _current_retrieval_trace.set(trace)
    try:
        yield trace
    finally:
        try:
            _current_retrieval_trace.reset(token)
        except ValueError:
            # StreamingResponse may resume/close the generator in a different context.
            # In that case, best-effort clear the current trace instead of failing the whole request.
            logger.warning('Retrieval trace token reset crossed context boundary; falling back to clear current trace')
            _current_retrieval_trace.set(None)


def get_current_retrieval_trace() -> RetrievalTrace | None:
    """返回当前上下文中绑定的检索溯源容器。"""

    return _current_retrieval_trace.get()


def preserve_stream_context(stream: Generator[str, None, None]) -> Generator[str, None, None]:
    """Advance and close an SSE generator in one context across threadpool yields.

    StreamingResponse may advance each segment in a different copied context.
    LangGraph tools must see the same turn-local trace for the entire run.
    """
    context = copy_context()
    try:
        while True:
            try:
                item = context.run(next, stream)
            except StopIteration:
                return
            yield item
    finally:
        context.run(stream.close)
