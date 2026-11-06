from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain.agents import create_agent

from app.agent.memory import get_checkpointer
from app.agent.prompts import RAG_AGENT_SYSTEM_PROMPT
from app.agent.tools import get_agent_tools
from app.core.llm import get_llm

"""
@Author  : 小滴课堂 【项目文档均已申请版权，盗用必究】
"""
@lru_cache(maxsize=1)
def get_rag_agent() -> Any:
    """构建并缓存 RAG Agent。

    根据官方 1.x 路线，这里使用 `langchain.agents.create_agent`，
    而不是旧的 `langgraph.prebuilt.create_react_agent`。

    这样做的好处：
    - 与 LangChain 1.x 的推荐用法保持一致；
    - 底层仍然运行在 LangGraph 上，天然支持持久化和流式事件；
    - 后续如果需要加 middleware、结构化输出或更多工具，扩展成本更低。
    """

    # 把模型、工具、系统提示词和记忆装配起来，执行循环由框架提供。
    # 模型可先提出工具调用，读取 ToolMessage 后继续回答；本项目只注册了知识库检索工具。
    return create_agent(
        model=get_llm(streaming=True, temperature=0.1),
        tools=get_agent_tools(),
        system_prompt=RAG_AGENT_SYSTEM_PROMPT,
        checkpointer=get_checkpointer(),
        name='rag_agent',
    )
