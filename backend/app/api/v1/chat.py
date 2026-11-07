from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.api.v1.ops import require_ops
from app.ops.metrics import SlaQuery

from app.schemas.chat import (
    ChatHistoryItem,
    ChatRequest,
    ChatResponse,
    SessionClearResponse,
    SessionSummaryItem,
)
from app.services.chat_service import ChatService
from app.agent.web_search import web_search_configured
from app.agent.runtime import preserve_stream_context

router = APIRouter(prefix='/chat', tags=['chat'])
"""
@Author  : 小滴课堂 【项目文档均已申请版权，盗用必究】
"""

def _ops_period(request: ChatRequest) -> SlaQuery | None:
    if request.mode != 'ops':
        return None
    require_ops()
    try:
        return SlaQuery.model_validate({
            'start_date': request.start_date, 'end_date': request.end_date, 'service_id': request.service_id,
        })
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail='请选择 1 至 90 天且已经结束的日期区间') from exc


@router.get('/capabilities')
def chat_capabilities() -> dict:
    """Report configuration without calling the search provider or exposing credentials."""
    return {'web_search': {'configured': web_search_configured(), 'provider': 'aliyun'}}


@router.post('', response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """同步问答接口。"""

    period = _ops_period(request)
    service = ChatService()
    try:
        return service.invoke(
            session_id=request.session_id,
            message=request.message,
            top_k=request.top_k, retrieval_mode=request.retrieval_mode, knowledge_base=request.knowledge_base,
            ops_period=period, reply_language=request.reply_language,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post('/stream')
def stream_chat(request: ChatRequest) -> StreamingResponse:
    """SSE 流式问答接口。"""

    period = _ops_period(request)
    service = ChatService()
    return StreamingResponse(
        preserve_stream_context(service.stream(
            session_id=request.session_id,
            message=request.message,
            top_k=request.top_k, retrieval_mode=request.retrieval_mode, knowledge_base=request.knowledge_base,
            ops_period=period, reply_language=request.reply_language,
        )),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


@router.get('/sessions', response_model=list[SessionSummaryItem])
def list_chat_sessions(
    limit: int = Query(default=50, ge=1, le=200, description='返回最近会话数量'),
) -> list[SessionSummaryItem]:
    """返回最近会话列表。"""

    service = ChatService()
    return service.list_sessions(limit=limit)


@router.get('/sessions/{session_id}/history', response_model=list[ChatHistoryItem])
def get_chat_session_history(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200, description='返回历史消息数量'),
) -> list[ChatHistoryItem]:
    """返回指定会话的问答历史。"""

    service = ChatService()
    return service.get_session_history(session_id, limit=limit)


@router.delete('/sessions/{session_id}', response_model=SessionClearResponse)
def clear_chat_session(session_id: str) -> SessionClearResponse:
    """清空指定会话的日志和短期记忆。"""

    service = ChatService()
    return service.clear_session(session_id)
