from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import get_settings
from app.core.deps import get_database
from app.graph.jobs import enqueue
from app.graph.retrieval import graph_paths
from app.models.document import Document
from app.models.chunk import Chunk
from app.models.graph import GraphJob, GraphState
from app.schemas.graph import GraphBuildRequest

router = APIRouter(prefix='/graph', tags=['graph'])


def job_item(job: GraphJob) -> dict:
    return {k: getattr(job, k) for k in ('id', 'document_id', 'version', 'knowledge_base', 'operation',
        'status', 'attempts', 'progress', 'total', 'error', 'model', 'schema_version', 'metrics', 'updated_at')}


@router.post('/build')
def build_graph(request: GraphBuildRequest, db: Session = Depends(get_database)) -> dict:
    if not get_settings().graph_enabled:
        raise HTTPException(409, '图谱功能尚未启用，请完成迁移并启动 worker')
    ids = sorted(set(request.document_ids))
    documents = db.scalars(select(Document).where(Document.id.in_(ids)).order_by(Document.id).with_for_update()).all()
    if len(documents) != len(ids):
        raise HTTPException(404, '部分文档不存在，请刷新列表')
    jobs = [enqueue(db, doc.id, explicit=True) for doc in documents]
    db.commit()
    return {'items': [job_item(job) for job in jobs]}


@router.get('/tasks')
def tasks(knowledge_base: str | None = Query(default=None, max_length=100),
          document_id: str | None = None, limit: int = Query(50, ge=1, le=100),
          offset: int = Query(0, ge=0), db: Session = Depends(get_database)) -> dict:
    statement = select(GraphJob).order_by(GraphJob.updated_at.desc(), GraphJob.id).limit(limit).offset(offset)
    if knowledge_base is not None:
        statement = statement.where(GraphJob.knowledge_base == knowledge_base)
    if document_id is not None:
        statement = statement.where(GraphJob.document_id == document_id)
    return {'enabled': get_settings().graph_enabled, 'items': [job_item(j) for j in db.scalars(statement)]}


@router.get('/states')
def states(limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0),
           db: Session = Depends(get_database)) -> dict:
    rows = db.scalars(select(GraphState).order_by(GraphState.document_id).limit(limit).offset(offset))
    return {'enabled': get_settings().graph_enabled, 'items': [
        {'document_id': s.document_id, 'knowledge_base': s.knowledge_base, 'status': s.status,
         'desired_version': s.desired_version, 'published_version': s.published_version} for s in rows]}


@router.get('/subgraph')
def subgraph(knowledge_base: str = Query(min_length=1, max_length=100),
             document_id: str | None = None, entity: str | None = Query(default=None, min_length=1, max_length=200),
             db: Session = Depends(get_database)) -> dict:
    if not get_settings().graph_enabled:
        return {'nodes': [], 'edges': [], 'graph_status': 'disabled', 'degradation_reason': '图谱功能未启用'}
    sources = select(Chunk.id).join(Document).where(Document.knowledge_base == knowledge_base,
                Chunk.enabled.is_(True)).order_by(Chunk.id).limit(10)
    if document_id:
        sources = sources.where(Document.id == document_id)
    try:
        paths = graph_paths(db, query=entity or '', chunk_ids=list(db.scalars(sources)),
                            knowledge_base=knowledge_base, document_id=document_id, entity=entity)
    except Exception as exc:
        raise HTTPException(503, '图谱暂不可用，请检查 Neo4j 服务') from exc
    nodes, edges = {}, {}
    for path in paths:
        new_nodes = {n['id']: n for n in path['entities']}
        new_edges = {a['id']: {**a, 'subject_id': a['subject_entity_id'], 'object_id': a['object_entity_id']}
                     for a in path['assertions']}
        if len(nodes.keys() | new_nodes.keys()) > 100 or len(edges.keys() | new_edges.keys()) > 200:
            continue
        nodes.update(new_nodes)
        edges.update(new_edges)
    return {'nodes': list(nodes.values()), 'edges': list(edges.values()), 'graph_status': 'ok' if edges else 'no_hits'}
