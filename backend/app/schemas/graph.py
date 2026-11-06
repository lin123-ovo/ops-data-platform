from __future__ import annotations

from typing import Annotated, Literal
from pydantic import BaseModel, Field, StringConstraints, model_validator


class RetrievalOptions(BaseModel):
    retrieval_mode: Literal['hybrid', 'graph_hybrid'] = 'hybrid'
    knowledge_base: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode='after')
    def graph_scope(self) -> 'RetrievalOptions':
        if self.knowledge_base is not None:
            self.knowledge_base = self.knowledge_base.strip()
            if not self.knowledge_base:
                raise ValueError('knowledge_base cannot be blank')
        if self.retrieval_mode == 'graph_hybrid' and not self.knowledge_base:
            raise ValueError('图谱检索必须指定知识库')
        return self


class GraphBuildRequest(BaseModel):
    document_ids: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=36)]] = Field(min_length=1, max_length=100)


class GraphFields(BaseModel):
    rank_graph: int | None = None
    graph_score: float | None = None
    graph_entities: list[dict] = Field(default_factory=list)
    graph_paths: list[dict] = Field(default_factory=list)


class GraphDiagnostics(BaseModel):
    graph_status: str = 'not_requested'
    degradation_reason: str | None = None
