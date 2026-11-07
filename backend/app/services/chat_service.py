from __future__ import annotations

import logging
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Generator, Literal

from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete, desc, func, select
from sqlalchemy.exc import SQLAlchemyError

from app.agent.memory import clear_thread_memory
from app.agent.graph import get_rag_agent
from app.agent.web_search import web_search_configured
from app.agent.runtime import RetrievalTrace, bind_retrieval_trace
from app.core.postgres import get_session_factory
from app.models.query_log import QueryLog
from app.models.graph import GraphChatMeta
from app.agent.runtime import get_current_retrieval_trace
from app.graph.extraction import digest
from app.ops.metrics import MetricSnapshot, OPS_KNOWLEDGE_BASE, OpsPeriod, UnknownService, get_metric
from app.rag.retriever import retrieve_chunks
from app.schemas.chat import (
    ChatHistoryItem,
    ChatResponse,
    SessionClearResponse,
    SessionSummaryItem,
    SourceChunkItem,
)
from app.utils.sse import format_sse_event

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ChatRunResult:
    """一次对话调用的标准化结果。"""

    session_id: str
    answer: str
    latency_ms: int
    source_chunks: list[dict]
    diagnostics: dict = field(default_factory=dict)
    thread_id: str = ''
    route: str = 'agent_rag'
    metric_snapshot: MetricSnapshot | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ChatService:
    """对话服务。

    这里把 Agent 调用、SSE 适配、日志记录统一放在 service 层，
    避免 API 层直接处理 LangChain / LangGraph 的细节。
    """

    def __init__(self) -> None:
        self.session_factory = get_session_factory()

    def _build_agent_input(
        self, message: str, prefetched_chunks: list[dict] | None = None, *,
        ops: bool = False, reply_language: Literal['en'] | None = None,
    ) -> dict:
        """把用户文本包装成 Agent 标准输入格式。"""

        # 把已检索片段拼进模型输入，再追加用户问题；模型据此进行有资料依据的生成。
        # 这一步是 Prompt 上下文组装，不是训练或微调模型。
        messages: list[dict[str, str]] = [{
            'role': 'system',
            'content': 'Current date in Asia/Hong_Kong: ' + datetime.now(ZoneInfo('Asia/Hong_Kong')).date().isoformat(),
        }]
        if not ops:
            messages[0]['content'] += (
                '\n联网工具 search_web 已接入阿里云百炼，当前配置状态：'
                + ('已启用，可调用。' if web_search_configured() else '未启用或未配置 DashScope Key。')
                + '需要实时信息时实际调用工具并根据结果说明，不自行推断访问权限。'
            )
        if prefetched_chunks:
            messages.append(
                {
                    'role': 'system',
                    'content': (
                        '以下是系统在回答前已从知识库中检索到的高相关片段，请优先基于这些内容回答。'
                        '如果这些片段与问题直接相关，请优先引用它们，并使用 `[1]`、`[2]` 这样的编号标记来源。\n\n'
                        f'{self._format_prefetched_context(prefetched_chunks)}'
                    ),
                }
            )
        if ops:
            # The card and its date range are rendered directly from the metric
            # service. The model only summarizes the retrieved runbook.
            message = (
                '请把本轮检索到的运维手册整理成 3 至 5 条 SLA 超时工单处置步骤。'
                '每条用 [编号] 标注原文依据，直接输出步骤，避免补充手册之外的流程。'
            )
        if reply_language == 'en':
            messages.append({'role': 'system', 'content': 'Write your response in English, even when the sources are in another language.'})
            if ops:
                message = (
                    'Summarize the retrieved runbook in three to five English steps for handling overdue SLA tickets. '
                    'Cite each step with its source number in brackets. Use only procedures supported by the runbook.'
                )
        messages.append({'role': 'user', 'content': message})
        return {'messages': messages}

    def _build_agent_config(self, session_id: str, *, ops: bool = False) -> dict:
        """构建 Agent 运行配置。

        根据 LangChain / LangGraph 1.x 的推荐方式，使用 `thread_id`
        作为多轮记忆的主键，把同一会话的上下文串起来。
        """

        trace = get_current_retrieval_trace()
        scope = trace.knowledge_base if trace else None
        thread = session_id + ':auto:v1'
        if scope is not None:
            thread += ':kb:' + digest(scope)[:16]
        if ops:
            thread = session_id + ':ops'
        return {'configurable': {'thread_id': thread}, 'recursion_limit': 24}

    def _extract_final_answer(self, result: dict) -> str:
        """从 Agent 返回状态中提取最后一条 AI 回复。"""

        messages = result.get('messages', [])
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                content = message.text if hasattr(message, 'text') else str(message.content)
                if content.strip():
                    return content.strip()
        return ''

    def _format_prefetched_context(self, prefetched_chunks: list[dict]) -> str:
        """把预检索到的片段压缩成适合模型消费的上下文。"""

        lines: list[str] = []
        for index, item in enumerate(prefetched_chunks[:5], start=1):
            lines.append(
                '\n'.join(
                    [
                        f'[{item.get("ref_id", index)}] filename={item.get("filename") or "unknown"}',
                        f'chunk_id={item.get("chunk_id") or "unknown"}',
                        f'chunk_index={item.get("chunk_index")}',
                        f'page_number={item.get("page_number")}',
                        f'retrieval_source={item.get("retrieval_source")}',
                        f'score={item.get("score")}',
                        f'content={str(item.get("content") or "")[:500]}',
                        'graph_paths=' + json.dumps(item.get('graph_paths', []), ensure_ascii=False),
                    ]
                )
            )
        return '\n\n'.join(lines)

    def _source_preview(self, source_chunks: list[dict]) -> list[dict]:
        return [
            {
                'ref_id': item.get('ref_id'),
                'chunk_id': item.get('chunk_id'),
                'filename': item.get('filename'),
                'retrieval_source': item.get('retrieval_source'),
                'score': item.get('score'),
                'vector_score': item.get('vector_score'),
                'bm25_score': item.get('bm25_score'),
                'fused_score': item.get('fused_score'),
            }
            for item in source_chunks[:3]
        ]

    def _normalize_source_chunks(self, source_chunks: list[dict]) -> list[dict]:
        """规范化来源字段，保证响应体和日志结构稳定。"""

        normalized_items: list[dict] = []
        for index, source_chunk in enumerate(source_chunks or [], start=1):
            normalized_items.append(
                {
                    **{k: source_chunk[k] for k in ('rank_graph', 'graph_score', 'graph_entities', 'graph_paths') if k in source_chunk},
                    'ref_id': int(source_chunk.get('ref_id', index)),
                    'chunk_id': source_chunk.get('chunk_id'),
                    'document_id': source_chunk.get('document_id'),
                    'filename': source_chunk.get('filename'),
                    'file_type': source_chunk.get('file_type'),
                    'chunk_index': source_chunk.get('chunk_index'),
                    'content': source_chunk.get('content', ''),
                    'score': float(source_chunk.get('score', 0.0)),
                    'vector_score': float(source_chunk['vector_score']) if source_chunk.get('vector_score') is not None else None,
                    'bm25_score': float(source_chunk['bm25_score']) if source_chunk.get('bm25_score') is not None else None,
                    'fused_score': float(source_chunk['fused_score']) if source_chunk.get('fused_score') is not None else None,
                    'retrieval_source': source_chunk.get('retrieval_source'),
                    'retrieval_sources': list(source_chunk.get('retrieval_sources') or []),
                    'rank_vector': source_chunk.get('rank_vector'),
                    'rank_bm25': source_chunk.get('rank_bm25'),
                    'rank_fused': source_chunk.get('rank_fused'),
                    'splitter_name': source_chunk.get('splitter_name'),
                    'parser_name': source_chunk.get('parser_name'),
                    'section_type': source_chunk.get('section_type'),
                    'section_title': source_chunk.get('section_title'),
                    'page_number': source_chunk.get('page_number'),
                    'source_path': source_chunk.get('source_path'),
                    'start_offset': source_chunk.get('start_offset'),
                    'end_offset': source_chunk.get('end_offset'),
                }
            )
        return normalized_items

    def _prefetch_source_chunks(self, *, message: str, top_k: int, trace: RetrievalTrace | None = None) -> list[dict]:
        """在进入 Agent 前先做一次确定性的知识库检索。"""

        # 同步/流式两条问答路径都会先经过这里；检索不完全依赖模型主动调用工具。
        # Agent 后续仍可调用 search_knowledge_base，因此一轮对话可能检索不止一次。
        diagnostics = {}
        with self.session_factory() as db:
            prefetched_hits = retrieve_chunks(db, query=message, top_k=top_k,
                retrieval_mode=trace.retrieval_mode if trace else 'hybrid',
                knowledge_base=trace.knowledge_base if trace else None, diagnostics=diagnostics)
        if trace is not None:
            trace.record_diagnostics(diagnostics)
            prefetched_hits = trace.register(prefetched_hits)
        normalized_hits = self._normalize_source_chunks(prefetched_hits)
        logger.info(
            '[CHAT] prefetch retrieval: query=%r top_k=%s hit_count=%s preview=%s',
            message,
            top_k,
            len(normalized_hits),
            self._source_preview(normalized_hits),
        )
        return normalized_hits

    def _persist_query_log(self, *, session_id: str, question: str, result: ChatRunResult) -> None:
        """把一次问答结果落到查询日志表。"""

        with self.session_factory() as db:
            query_log = QueryLog(
                session_id=session_id,
                user_question=question,
                answer=result.answer,
                route=result.route,
                latency_ms=result.latency_ms,
                source_chunks=result.source_chunks,
            )
            db.add(query_log)
            db.flush()
            diagnostics = dict(result.diagnostics)
            if result.metric_snapshot is not None:
                diagnostics['metric_snapshot'] = result.metric_snapshot.model_dump(mode='json')
            db.add(GraphChatMeta(query_log_id=query_log.id, diagnostics=diagnostics, thread_id=result.thread_id))
            db.commit()

    def _serialize_history_item(self, query_log: QueryLog) -> ChatHistoryItem:
        """把数据库查询日志转成接口层响应对象。"""

        source_chunks = self._normalize_source_chunks(query_log.source_chunks)
        with self.session_factory() as db:
            meta = db.get(GraphChatMeta, query_log.id)
            diagnostics = meta.diagnostics if meta else {}
        return ChatHistoryItem(
            **diagnostics,
            id=query_log.id,
            session_id=query_log.session_id,
            user_question=query_log.user_question,
            answer=query_log.answer,
            route=query_log.route,
            latency_ms=query_log.latency_ms,
            source_chunks=[SourceChunkItem.model_validate(item) for item in source_chunks],
            created_at=query_log.created_at,
            updated_at=query_log.updated_at,
        )

    @contextmanager
    def _chat_run(self, *, top_k: int, retrieval_mode: str = 'hybrid', knowledge_base: str | None = None, reply_language: str | None = None) -> Generator[RetrievalTrace, None, None]:
        """为一次问答绑定独立的检索溯源容器。"""

        trace = RetrievalTrace(top_k=top_k, retrieval_mode=retrieval_mode, knowledge_base=knowledge_base, reply_language=reply_language)
        with bind_retrieval_trace(trace):
            yield trace

    def _ops_metric(self, period: OpsPeriod) -> MetricSnapshot:
        try:
            with self.session_factory() as db:
                return get_metric(db, period)
        except (SQLAlchemyError, UnknownService) as exc:
            logger.warning('Ops chat metric query failed: %s', type(exc).__name__)
            return MetricSnapshot(status='unavailable', **period.model_dump())

    def invoke(self, *, session_id: str, message: str, top_k: int = 5, retrieval_mode: str = 'hybrid', knowledge_base: str | None = None, ops_period: OpsPeriod | None = None, reply_language: Literal['en'] | None = None) -> ChatResponse:
        """执行一次同步问答。"""

        started_at = time.perf_counter()
        logger.info(
            '[CHAT] invoke started: session=%s top_k=%s message_length=%s message_preview=%r',
            session_id,
            top_k,
            len(message),
            message[:120],
        )
        metric = self._ops_metric(ops_period) if ops_period else None
        if metric is not None:
            knowledge_base = OPS_KNOWLEDGE_BASE
        try:
            with self._chat_run(top_k=top_k, retrieval_mode=retrieval_mode, knowledge_base=knowledge_base, reply_language=reply_language) as trace:
                config = self._build_agent_config(session_id, ops=metric is not None)
                prefetched_chunks = self._prefetch_source_chunks(
                    message=f'{message} SLA 超时工单处置流程' if reply_language == 'en' else message,
                    top_k=top_k, trace=trace,
                ) if metric is not None else []
                if metric is not None and not prefetched_chunks:
                    answer = 'No relevant operations runbook was found.' if reply_language == 'en' else '未检索到相关运维手册。'
                else:
                    result = get_rag_agent(ops=metric is not None).invoke(
                        self._build_agent_input(
                            message, prefetched_chunks, ops=metric is not None, reply_language=reply_language,
                        ), config=config,
                    )
                    answer = self._extract_final_answer(result)
        except Exception as exc:  # noqa: BLE001
            logger.exception('Chat invoke failed for session=%s', session_id)
            if metric is None:
                raise RuntimeError(f'Agent 对话执行失败: {exc}') from exc
            answer = 'Could not generate runbook guidance. Try again.' if reply_language == 'en' else '运维建议生成失败，请稍后重试。'

        latency_ms = int((time.perf_counter() - started_at) * 1000)
        source_chunks = self._normalize_source_chunks(trace.source_chunks)
        chat_result = ChatRunResult(
            session_id=session_id,
            answer=answer,
            latency_ms=latency_ms,
            source_chunks=source_chunks, diagnostics={**trace.diagnostics, **trace.web_snapshot()},
            thread_id=config['configurable']['thread_id'],
            route='agent_ops' if metric is not None else trace.route(),
            metric_snapshot=metric,
        )
        self._persist_query_log(session_id=session_id, question=message, result=chat_result)
        logger.info(
            '[CHAT] invoke finished: session=%s latency_ms=%s answer_length=%s source_chunk_count=%s source_preview=%s',
            session_id,
            latency_ms,
            len(answer),
            len(source_chunks),
            self._source_preview(source_chunks),
        )

        return ChatResponse(
            **trace.diagnostics, **trace.web_snapshot(),
            session_id=session_id,
            answer=answer,
            latency_ms=latency_ms,
            source_chunks=[SourceChunkItem.model_validate(item) for item in source_chunks],
            created_at=chat_result.created_at,
            route=chat_result.route,
            metric_snapshot=metric,
        )

    def stream(self, *, session_id: str, message: str, top_k: int = 5, retrieval_mode: str = 'hybrid', knowledge_base: str | None = None, ops_period: OpsPeriod | None = None, reply_language: Literal['en'] | None = None) -> Generator[str, None, None]:
        """以 SSE 方式流式返回问答结果。"""

        started_at = time.perf_counter()
        answer_fragments: list[str] = []
        metric = self._ops_metric(ops_period) if ops_period else None
        if metric is not None:
            knowledge_base = OPS_KNOWLEDGE_BASE
        trace = RetrievalTrace(top_k=top_k, retrieval_mode=retrieval_mode, knowledge_base=knowledge_base, reply_language=reply_language)

        logger.info(
            '[CHAT] stream started: session=%s top_k=%s message_length=%s message_preview=%r',
            session_id,
            top_k,
            len(message),
            message[:120],
        )
        yield format_sse_event('status', {'phase': 'started', 'session_id': session_id})
        if metric is not None:
            yield format_sse_event('metric_snapshot', metric.model_dump(mode='json'))
        with bind_retrieval_trace(trace):
            config = self._build_agent_config(session_id, ops=metric is not None)
        try:
            prefetched_chunks = self._prefetch_source_chunks(
                message=f'{message} SLA 超时工单处置流程' if reply_language == 'en' else message,
                top_k=top_k, trace=trace,
            ) if metric is not None else []
            if prefetched_chunks:
                trace.register(prefetched_chunks)
                yield format_sse_event(
                    'sources',
                    {
                        'items': prefetched_chunks, **trace.diagnostics,
                    },
                )
                yield format_sse_event(
                    'status',
                    {
                        'phase': 'retrieved',
                        'session_id': session_id,
                        'source_chunk_count': len(prefetched_chunks),
                    },
                )
            if metric is not None and not prefetched_chunks:
                answer_fragments.append(
                    'No relevant operations runbook was found.' if reply_language == 'en' else '未检索到相关运维手册。',
                )
                yield format_sse_event('token', {'text': answer_fragments[0]})
            else:
                with bind_retrieval_trace(trace):
                    yield from self._stream_agent(
                        message, prefetched_chunks, config, answer_fragments,
                        ops=metric is not None, reply_language=reply_language,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.exception('Chat stream failed for session=%s', session_id)
            if metric is None:
                yield format_sse_event('error', {
                    'session_id': session_id, 'message': f'Agent 流式对话执行失败: {exc}',
                })
                yield format_sse_event('done', {'session_id': session_id, 'ok': False})
                return
            answer_fragments[:] = [
                'Could not generate runbook guidance. Try again.' if reply_language == 'en' else '运维建议生成失败，请稍后重试。',
            ]

        answer = ''.join(answer_fragments).strip()
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        source_chunks = self._normalize_source_chunks(trace.source_chunks)
        logger.info(
            '[CHAT] stream finished: session=%s latency_ms=%s answer_length=%s source_chunk_count=%s source_preview=%s',
            session_id,
            latency_ms,
            len(answer),
            len(source_chunks),
            self._source_preview(source_chunks),
        )
        chat_result = ChatRunResult(
            session_id=session_id,
            answer=answer,
            latency_ms=latency_ms,
            source_chunks=source_chunks, diagnostics={**trace.diagnostics, **trace.web_snapshot()},
            thread_id=config['configurable']['thread_id'],
            route='agent_ops' if metric is not None else trace.route(),
            metric_snapshot=metric,
        )
        self._persist_query_log(session_id=session_id, question=message, result=chat_result)

        yield format_sse_event(
            'sources',
            {
                'items': source_chunks, **trace.diagnostics,
            },
        )
        yield format_sse_event('web_sources', trace.web_snapshot())
        yield format_sse_event(
            'done',
            {
                'session_id': session_id,
                'answer': answer,
                'route': chat_result.route,
                'latency_ms': latency_ms,
                'created_at': chat_result.created_at.isoformat(),
                'source_chunks': source_chunks, **trace.diagnostics, **trace.web_snapshot(),
            },
        )

    def _stream_agent(
        self, message: str, prefetched_chunks: list[dict], config: dict,
        answer_fragments: list[str], *, ops: bool, reply_language: Literal['en'] | None = None,
    ) -> Generator[str, None, None]:
        # messages 输出增量文本；updates 输出模型/工具节点的状态更新。
        # 后续统一转换成 SSE 事件，让前端同时展示答案、调用过程和引用来源。
        for stream_mode, chunk in get_rag_agent(ops=ops).stream(
            self._build_agent_input(message, prefetched_chunks, ops=ops, reply_language=reply_language),
            config=config,
            stream_mode=['updates', 'messages'],
        ):
            if stream_mode == 'messages':
                token, metadata = chunk
                if metadata.get('langgraph_node') != 'model':
                    continue
                content_blocks = getattr(token, 'content_blocks', []) or []
                for block in content_blocks:
                    if block.get('type') != 'text':
                        continue

                    text_delta = block.get('text', '')
                    if not text_delta:
                        continue

                    answer_fragments.append(text_delta)
                    yield format_sse_event('token', {'text': text_delta})
                continue

            if stream_mode != 'updates':
                continue

            update_chunk = chunk
            for step_name, step_data in update_chunk.items():
                messages = (step_data or {}).get('messages', [])
                if not messages:
                    continue

                for latest_message in messages:
                    if isinstance(latest_message, AIMessage):
                        tool_calls = latest_message.tool_calls or []
                        if not tool_calls and latest_message.text.strip():
                            answer_fragments[:] = [latest_message.text.strip()]
                        for tool_call in tool_calls:
                            yield format_sse_event(
                                'tool_call',
                                {
                                    'step': step_name,
                                    'tool_name': tool_call.get('name'),
                                    'tool_call_id': tool_call.get('id'),
                                    'args': tool_call.get('args'),
                                },
                            )
                    elif isinstance(latest_message, ToolMessage):
                        tool_status = getattr(latest_message, 'status', 'success') or 'success'
                        artifact = getattr(latest_message, 'artifact', None)
                        if isinstance(artifact, dict) and artifact.get('error'):
                            tool_status = 'error'
                        yield format_sse_event(
                            'tool_error' if tool_status == 'error' else 'tool_result',
                            {
                                'step': step_name,
                                'tool_name': latest_message.name,
                                'tool_call_id': latest_message.tool_call_id,
                                'status': tool_status,
                                'content': str(latest_message.content),
                            },
                        )

                trace = get_current_retrieval_trace()
                if trace is not None and any(isinstance(item, ToolMessage) for item in messages):
                    yield format_sse_event('sources', {'items': self._normalize_source_chunks(trace.source_chunks), **trace.diagnostics})
                    yield format_sse_event('web_sources', trace.web_snapshot())

    def clear_session(self, session_id: str) -> SessionClearResponse:
        """清空会话日志，并尝试清空 Agent 短期记忆。"""

        with self.session_factory() as db:
            log_ids = select(QueryLog.id).where(QueryLog.session_id == session_id)
            thread_ids = list(db.scalars(select(GraphChatMeta.thread_id).where(GraphChatMeta.query_log_id.in_(log_ids))))
            db.execute(delete(GraphChatMeta).where(GraphChatMeta.query_log_id.in_(log_ids)))
            statement = delete(QueryLog).where(QueryLog.session_id == session_id)
            result = db.execute(statement)
            db.commit()

        cleared_memory = False
        try:
            results = [clear_thread_memory(t) for t in set([session_id, *thread_ids]) if t]
            cleared_memory = all(results)
        except Exception as exc:  # noqa: BLE001
            logger.warning('Failed to clear thread memory for session=%s: %s', session_id, exc)

        return SessionClearResponse(
            session_id=session_id,
            deleted_query_log_count=int(result.rowcount or 0),
            cleared_memory=cleared_memory,
        )

    def get_session_history(self, session_id: str, *, limit: int = 50) -> list[ChatHistoryItem]:
        """按会话 ID 返回最近的问答历史。"""

        with self.session_factory() as db:
            statement = (
                select(QueryLog)
                .where(QueryLog.session_id == session_id)
                .order_by(QueryLog.created_at.asc())
                .limit(limit)
            )
            query_logs = db.execute(statement).scalars().all()
        return [self._serialize_history_item(query_log) for query_log in query_logs]

    def list_sessions(self, *, limit: int = 50) -> list[SessionSummaryItem]:
        """返回会话摘要列表，便于前端展示最近会话。"""

        with self.session_factory() as db:
            latest_created_at_subquery = (
                select(
                    QueryLog.session_id.label('session_id'),
                    func.max(QueryLog.created_at).label('latest_created_at'),
                    func.count(QueryLog.id).label('message_count'),
                )
                .where(QueryLog.session_id.is_not(None))
                .group_by(QueryLog.session_id)
                .subquery()
            )

            statement = (
                select(QueryLog, latest_created_at_subquery.c.message_count)
                .join(
                    latest_created_at_subquery,
                    (QueryLog.session_id == latest_created_at_subquery.c.session_id)
                    & (QueryLog.created_at == latest_created_at_subquery.c.latest_created_at),
                )
                .order_by(desc(QueryLog.created_at))
                .limit(limit)
            )
            rows = db.execute(statement).all()

        return [
            SessionSummaryItem(
                session_id=query_log.session_id or '',
                latest_question=query_log.user_question,
                latest_answer=query_log.answer,
                message_count=int(message_count or 0),
                updated_at=query_log.updated_at,
            )
            for query_log, message_count in rows
            if query_log.session_id
        ]
