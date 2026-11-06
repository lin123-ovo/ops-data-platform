"""Single-job worker: python -m app.graph.worker. Leases fence late workers."""
from __future__ import annotations

import logging
import signal
import threading
import time
import uuid
from sqlalchemy import or_, select
from app.config import get_settings
from app.core.postgres import get_session_factory
from app.graph.extraction import extract
from app.graph.jobs import snapshot
from app.graph.store import get_graph_store
from app.models.graph import GraphJob, GraphState

logger = logging.getLogger(__name__)


def claim(factory) -> str | None:
    now = time.time()
    with factory() as db, db.begin():
        job = db.scalar(select(GraphJob).where(
            or_(GraphJob.status == 'pending',
                (GraphJob.status == 'processing') & (GraphJob.lease_until < now)),
            GraphJob.lease_until < now,
        ).order_by(GraphJob.created_at).with_for_update(skip_locked=True).limit(1))
        if job is None:
            return None
        state = db.get(GraphState, job.document_id)
        if state is None or state.desired_version != job.version:
            job.status = 'expired'
            return None
        if job.attempts >= get_settings().graph_max_attempts:
            job.status, job.error, state.status = 'failed', '任务租约多次过期，请检查 worker 后重试', 'failed'
            return None
        job.token, job.status = str(uuid.uuid4()), 'processing'
        job.attempts += 1
        job.progress = 0
        job.lease_until = now + get_settings().graph_lease_seconds
        state.status = 'processing'
        return job.id


def process(job_id: str, factory, store=None, extractor=extract) -> None:
    settings = get_settings()
    with factory() as db:
        job = db.get(GraphJob, job_id)
        doc_id, version, token, operation = job.document_id, job.version, job.token, job.operation
        current_version, chunks, kb = snapshot(db, doc_id)
    started, usage, relation_count = time.monotonic(), {}, 0
    try:
        if current_version != version:
            raise ValueError('source_version_changed')
        store = store or get_graph_store()
        store.initialize()
        if operation == 'delete':
            store.cleanup(doc_id)
        else:
            for index, chunk in enumerate(chunks, start=1):
                if len(chunk['content']) > settings.graph_max_chunk_chars:
                    raise ValueError('Chunk 超过图谱抽取长度限制，请细分后重建')
                with factory() as db, db.begin():
                    job = db.scalar(select(GraphJob).where(GraphJob.id == job_id).with_for_update())
                    state = db.get(GraphState, doc_id)
                    if job.token != token or state.desired_version != version or job.lease_until < time.time():
                        raise ValueError('source_version_changed')
                    job.lease_until = time.time() + settings.graph_lease_seconds
                # No database transaction or row lock spans either network call.
                extracted, chunk_usage = extractor(chunk['content'])
                rows = extracted.validated_rows(content=chunk['content'], chunk_id=chunk['id'],
                    document_id=doc_id, knowledge_base=kb, version=version, token=token)
                store.write(rows)
                relation_count += len(rows)
                for key, value in chunk_usage.items():
                    if isinstance(value, (int, float)):
                        usage[key] = usage.get(key, 0) + value
                with factory() as db, db.begin():
                    job = db.get(GraphJob, job_id)
                    if job.token != token:
                        raise ValueError('source_version_changed')
                    job.progress = index
        with factory() as db, db.begin():
            state = db.scalar(select(GraphState).where(GraphState.document_id == doc_id).with_for_update())
            job = db.scalar(select(GraphJob).where(GraphJob.id == job_id).with_for_update())
            if job.token != token or job.lease_until < time.time() or state.desired_version != version:
                raise ValueError('source_version_changed')
            if snapshot(db, doc_id)[0] != version:
                raise ValueError('source_version_changed')
            state.published_version, state.published_token, state.status = version, token, 'succeeded'
            job.status, job.error = 'succeeded', None
            job.metrics = {'elapsed_ms': round((time.monotonic()-started)*1000),
                           'relations': relation_count, 'token_usage': usage}
        with factory() as db:
            state = db.get(GraphState, doc_id)
            protected = [state.published_token] + list(db.scalars(select(GraphJob.token).where(
                GraphJob.document_id == doc_id, GraphJob.status == 'processing')))
        try:
            store.cleanup_obsolete(doc_id, protected)
        except Exception as exc:
            logger.warning('Graph obsolete cleanup deferred: job=%s category=%s', job_id, type(exc).__name__)
    except Exception as exc:
        logger.error('Graph job failed: job=%s category=%s', job_id, type(exc).__name__)
        with factory() as db, db.begin():
            job = db.get(GraphJob, job_id)
            state = db.get(GraphState, doc_id)
            if job.token != token:
                return
            if state.desired_version != version or str(exc) == 'source_version_changed':
                job.status = 'expired'
            else:
                job.status = 'failed' if job.attempts >= settings.graph_max_attempts else 'pending'
                job.lease_until = time.time() + min(30, 2 ** job.attempts)
            # Do not persist provider exception bodies (which may contain credentials or text).
            job.error = '资料已变化，任务已过期' if job.status == 'expired' else f'建图失败（{type(exc).__name__}），可重试并检查服务配置'
            job.metrics = {'elapsed_ms': round((time.monotonic()-started)*1000),
                           'relations': relation_count, 'token_usage': usage}
            if state.desired_version == version:
                state.status = job.status


def main() -> None:
    if not get_settings().graph_enabled:
        raise SystemExit('请先配置 GRAPH_ENABLED=true，并执行图谱迁移预检')
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    factory = get_session_factory()
    while not stop.is_set():
        try:
            job_id = claim(factory)
            if job_id:
                process(job_id, factory)
                continue
        except Exception as exc:
            logger.error('Graph worker queue unavailable: %s', type(exc).__name__)
        stop.wait(2)
    get_graph_store().close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
