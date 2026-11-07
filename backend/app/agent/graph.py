from __future__ import annotations

from functools import lru_cache
import re
from typing import Any

from langchain.agents import AgentState, create_agent
from langchain.agents.middleware import after_model
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
from langgraph.runtime import Runtime

from app.agent.memory import get_checkpointer
from app.agent.prompts import RAG_AGENT_SYSTEM_PROMPT
from app.agent.tools import get_agent_tools
from app.agent.runtime import get_current_retrieval_trace
from app.core.llm import get_llm

"""
@Author  : 小滴课堂 【项目文档均已申请版权，盗用必究】
"""
@after_model(can_jump_to=['model', 'end'])
def validate_citations(state: AgentState, runtime: Runtime) -> dict[str, Any] | None:
    """Give the model one chance to retrieve missing per-turn citation evidence."""
    message = state['messages'][-1]
    trace = get_current_retrieval_trace()
    if trace is None or not isinstance(message, AIMessage) or message.tool_calls:
        return None
    cited = set(re.findall(r'\[(W?\d+)\]', message.text))
    known = {str(source['ref_id']) for source in trace.source_chunks + trace.web_sources}
    if cited <= known:
        return None
    if trace.citation_corrections:
        answer = (
            'Could not verify the sources for this response. Please retry.'
            if trace.reply_language == 'en' else '无法核验本轮回答的引用来源，请重试。'
        )
        return {
            'messages': [AIMessage(id=message.id, content=answer)],
            'jump_to': 'end',
        }
    trace.citation_corrections += 1
    return {
        'messages': [
            RemoveMessage(id=message.id),
            HumanMessage(content=(
                '系统引用校验失败。请重新回答用户刚才的问题：必须先调用检索工具取得本轮来源，'
                '知识库或文件问题使用 search_knowledge_base，最新公开事实使用可用的 search_web。'
                '之前轮次的编号不能直接引用；若工具失败或没有依据，说明无法核实，不编造编号。'
            )),
        ],
        'jump_to': 'model',
    }


@lru_cache(maxsize=2)
def get_rag_agent(*, ops: bool = False) -> Any:
    """构建并缓存 RAG Agent。

    根据官方 1.x 路线，这里使用 `langchain.agents.create_agent`，
    而不是旧的 `langgraph.prebuilt.create_react_agent`。

    这样做的好处：
    - 与 LangChain 1.x 的推荐用法保持一致；
    - 底层仍然运行在 LangGraph 上，天然支持持久化和流式事件；
    - 后续如果需要加 middleware、结构化输出或更多工具，扩展成本更低。
    """

    # 把模型、工具、系统提示词和记忆装配起来，执行循环由框架提供。
    # 普通对话由模型选择知识库或联网工具，运维对话只注册知识库工具。
    return create_agent(
        model=get_llm(streaming=True, temperature=0.1),
        tools=get_agent_tools(ops=ops),
        system_prompt=RAG_AGENT_SYSTEM_PROMPT + (
            '\n当前为运维模式，仅使用内部指标与运维手册，联网工具不可用。' if ops else ''
        ),
        checkpointer=get_checkpointer(),
        middleware=[validate_citations],
        name='rag_agent',
    )
