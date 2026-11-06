from __future__ import annotations

from sqlalchemy.orm import Session

from app.rag.retriever import retrieve_chunks


class RetrievalService:
    """检索调试相关业务编排。"""

    def __init__(self, db: Session) -> None:
        self.db = db

    def search(self, *, query: str, top_k: int = 5, retrieval_mode: str = 'hybrid', knowledge_base: str | None = None, diagnostics: dict | None = None) -> list[dict]:
        """返回最基础的检索结果列表。"""

        return retrieve_chunks(self.db, query=query, top_k=top_k, retrieval_mode=retrieval_mode, knowledge_base=knowledge_base, diagnostics=diagnostics)
