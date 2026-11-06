"""Transactional queue and version fencing shared by mutation services and worker."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.config import get_settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.graph import GraphJob, GraphState
from app.graph.extraction import SCHEMA_VERSION, digest


def snapshot(db: Session, document_id: str) -> tuple[str, list[dict], str]:
    document = db.get(Document, document_id)
    if document is None:
        return 'deleted', [], ''
    chunks = db.scalars(select(Chunk).where(Chunk.document_id == document_id).order_by(Chunk.id)).all()
    rows = [{'id': c.id, 'content': c.content, 'enabled': c.enabled, 'metadata': c.metadata_json}
            for c in chunks]
    version = digest([document.knowledge_base, rows, SCHEMA_VERSION, get_settings().model])
    return version, [c for c in rows if c['enabled']], document.knowledge_base


def enqueue(db: Session, document_id: str, *, explicit: bool = False) -> GraphJob | None:
    """Enqueue before caller commit, preserving a tombstone after source deletion."""
    if not get_settings().graph_enabled and not explicit:
        return None
    db.flush()
    # Serialize automatic updates with manual builds, including first-state creation.
    db.scalar(select(Document).where(Document.id == document_id).with_for_update())
    state = db.scalar(select(GraphState).where(GraphState.document_id == document_id).with_for_update())
    version, chunks, kb = snapshot(db, document_id)
    if state is None:
        if version == 'deleted':
            return None
        state = GraphState(document_id=document_id, knowledge_base=kb, desired_version=version)
        db.add(state)
    if version != state.desired_version:
        state.published_token = None
        state.published_version = None
    state.desired_version = version
    if kb:
        state.knowledge_base = kb
    operation = 'delete' if version == 'deleted' else 'build'
    job_id = digest([document_id, version])
    job = db.get(GraphJob, job_id)
    if job is None:
        job = GraphJob(id=job_id, document_id=document_id, version=version,
                       knowledge_base=state.knowledge_base, operation=operation,
                       model=get_settings().model, schema_version=SCHEMA_VERSION, total=len(chunks))
        db.add(job)
        state.status = 'pending'
    elif (explicit and job.status in {'failed', 'expired'}) or (job.status == 'succeeded' and state.published_token is None):
        job.status, job.attempts, job.error, job.lease_until = 'pending', 0, None, 0
        state.status = 'pending'
    elif state.published_version != version:
        state.status = job.status
    return job
