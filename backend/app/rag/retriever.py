from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter

from sqlalchemy import select
from app.models.document import Document
from app.config import get_settings
from sqlalchemy.orm import Session

from app.core.milvus_client import get_vector_store
from app.core.vector_errors import VectorError, vector_event, vector_operation
from app.models.chunk import Chunk
from app.rag.bm25_index import get_bm25_index

logger = logging.getLogger(__name__)

# RRF 的平滑常数 60 与用户的 top_k 含义不同；top_k 是最终返回多少个片段。
_RRF_K = 60
_CANDIDATE_MULTIPLIER = 4
_MIN_CANDIDATE_K = 10


def _preview_hits(hits: list[dict], score_field: str = 'score') -> list[dict]:
    return [
        {
            'chunk_id': item.get('chunk_id'),
            'filename': item.get('filename'),
            'score': round(float(item.get(score_field) or 0.0), 4),
            'retrieval_source': item.get('retrieval_source'),
            'content_preview': str(item.get('content') or '')[:120],
        }
        for item in hits[:3]
    ]


def _candidate_k(top_k: int) -> int:
    return max(top_k * _CANDIDATE_MULTIPLIER, _MIN_CANDIDATE_K)


def _rrf_score(rank: int) -> float:
    # 每一路中排第 rank 名就贡献 1/(60+rank)，同一 Chunk 的两路贡献相加。
    # 例如两路都排第一的片段得 2/61，避免直接相加不同量纲的原始分数。
    return 1.0 / (_RRF_K + rank)


@vector_operation()
def _vector_retrieve_chunks(*, query: str, candidate_k: int, knowledge_base: str | None = None) -> list[dict]:
    started = perf_counter()
    try:
        with vector_operation():
            vector_store = get_vector_store()
            docs_with_score = vector_store.similarity_search_with_score(query, k=candidate_k, **({"knowledge_base": knowledge_base} if knowledge_base is not None else {}))
            vector_event('retrieval', started, count=len(docs_with_score))
        logger.info(
            '[VECTOR] retrieval completed: query=%r candidate_k=%s raw_hit_count=%s',
            query,
            candidate_k,
            len(docs_with_score),
        )
    except VectorError as exc:
        vector_event('retrieval', started, status='failed', category=exc.category, count=0)
        return []
    except Exception as exc:  # noqa: BLE001
        vector_event('retrieval', started, status='failed', category=type(exc).__name__, count=0)
        return []

    hits: list[dict] = []
    for rank, (document, score) in enumerate(docs_with_score, start=1):
        metadata = document.metadata or {}
        numeric_score = float(score)
        hits.append(
            {
                'chunk_id': metadata.get('chunk_id'),
                'document_id': metadata.get('document_id'),
                'filename': metadata.get('filename'),
                'file_type': metadata.get('file_type'),
                'chunk_index': metadata.get('chunk_index'),
                'content': document.page_content,
                'score': numeric_score,
                'vector_score': numeric_score,
                'index_vector_id': metadata.get('pk'),
                'splitter_name': metadata.get('splitter_name'),
                'parser_name': metadata.get('parser_name'),
                'section_type': metadata.get('section_type'),
                'section_title': metadata.get('section_title'),
                'page_number': metadata.get('page_number'),
                'source_path': metadata.get('source_path'),
                'start_offset': metadata.get('start_offset'),
                'end_offset': metadata.get('end_offset'),
                'retrieval_source': 'vector',
                'retrieval_sources': ['vector'],
                'rank_vector': rank,
            }
        )

    logger.info(
        '[VECTOR] retrieval preview: query=%r hit_count=%s preview=%s',
        query,
        len(hits),
        _preview_hits(hits, score_field='vector_score'),
    )
    return hits


def _bm25_retrieve_chunks(*, query: str, candidate_k: int, knowledge_base: str | None = None) -> list[dict]:
    try:
        hits = get_bm25_index().search(query, top_k=candidate_k, candidate_k=candidate_k, **({"knowledge_base": knowledge_base} if knowledge_base is not None else {}))
    except Exception as exc:  # noqa: BLE001
        logger.exception('[BM25] retrieval failed: query=%r candidate_k=%s error=%s', query, candidate_k, exc)
        return []

    logger.info(
        '[BM25] retrieval preview: query=%r hit_count=%s preview=%s',
        query,
        len(hits),
        _preview_hits(hits, score_field='bm25_score'),
    )
    return hits


def _fuse_hits(*, query: str, vector_hits: list[dict], bm25_hits: list[dict], top_k: int, graph_hits: list[dict] | None = None) -> list[dict]:
    merged_by_chunk_id: dict[str, dict] = {}

    for source_name, hits in (('vector', vector_hits), ('bm25', bm25_hits), ('graph', graph_hits or [])):
        for rank, hit in enumerate(hits, start=1):
            # 按 Chunk 主键合并：同一片段被两路命中时只返回一份，并累计排名贡献。
            chunk_id = str(hit.get('chunk_id') or f'{source_name}:{rank}')
            fused_hit = merged_by_chunk_id.setdefault(
                chunk_id,
                {
                    **hit,
                    'score': float(hit.get('score') or 0.0),
                    'vector_score': hit.get('vector_score'),
                    'bm25_score': hit.get('bm25_score'),
                    'fused_score': 0.0,
                    'retrieval_sources': [],
                    'rank_vector': None,
                    'rank_bm25': None,
                    'rank_fused': None,
                },
            )

            if source_name not in fused_hit['retrieval_sources']:
                fused_hit['retrieval_sources'].append(source_name)

            fused_hit['fused_score'] += _rrf_score(rank)
            if source_name == 'vector':
                fused_hit['vector_score'] = hit.get('vector_score')
                fused_hit['rank_vector'] = rank
            elif source_name == 'bm25':
                fused_hit['bm25_score'] = hit.get('bm25_score')
                fused_hit['rank_bm25'] = rank
            else:
                fused_hit['rank_graph'] = rank
                fused_hit['graph_score'] = hit.get('graph_score')
                fused_hit['graph_paths'] = hit.get('graph_paths', [])
                fused_hit['graph_entities'] = hit.get('graph_entities', [])

            for field_name, value in hit.items():
                if field_name in {'retrieval_sources', 'retrieval_source'}:
                    continue
                if fused_hit.get(field_name) in (None, '', []) and value not in (None, '', []):
                    fused_hit[field_name] = value

    ranked_hits = sorted(
        merged_by_chunk_id.values(),
        key=lambda item: (
            float(item.get('fused_score') or 0.0),
            float(item.get('bm25_score') or 0.0),
            float(item.get('vector_score') or 0.0),
        ),
        reverse=True,
    )

    for rank, hit in enumerate(ranked_hits, start=1):
        hit['rank_fused'] = rank
        if len(hit['retrieval_sources']) > 1:
            hit['retrieval_source'] = 'hybrid'
            hit['score'] = float(hit.get('fused_score') or 0.0)
        elif hit['retrieval_sources'] == ['bm25']:
            hit['retrieval_source'] = 'bm25'
            hit['score'] = float(hit.get('bm25_score') or 0.0)
        elif hit['retrieval_sources'] == ['graph']:
            hit['retrieval_source'] = 'graph'
            hit['score'] = float(hit.get('fused_score') or 0.0)
        else:
            hit['retrieval_source'] = 'vector'
            hit['score'] = float(hit.get('vector_score') or 0.0)

    fused_hits = ranked_hits[:top_k]
    logger.info(
        '[HYBRID] fusion completed: query=%r vector_hits=%s bm25_hits=%s final_hits=%s preview=%s',
        query,
        len(vector_hits),
        len(bm25_hits),
        len(fused_hits),
        [
            {
                'chunk_id': item.get('chunk_id'),
                'filename': item.get('filename'),
                'retrieval_source': item.get('retrieval_source'),
                'vector_score': round(float(item.get('vector_score') or 0.0), 4),
                'bm25_score': round(float(item.get('bm25_score') or 0.0), 4),
                'fused_score': round(float(item.get('fused_score') or 0.0), 4),
                'content_preview': str(item.get('content') or '')[:120],
            }
            for item in fused_hits[:3]
        ],
    )
    return fused_hits


def _postgres_fallback(db: Session, *, query: str, top_k: int, knowledge_base: str | None = None) -> list[dict]:
    # 兜底按整个问题做 ILIKE 子串匹配，能力比 BM25/语义检索弱，不是另一个 AI 模型。
    statement = (
        select(Chunk).join(Document)
        .where(Chunk.enabled.is_(True), Chunk.content.ilike(f'%{query}%'))
        .order_by(Chunk.updated_at.desc())
        .limit(top_k)
    )
    if knowledge_base is not None:
        statement = statement.where(Document.knowledge_base == knowledge_base)
    chunks = db.execute(statement).scalars().all()
    fallback_hits = [
        {
            'chunk_id': chunk.id,
            'document_id': chunk.document_id,
            'filename': chunk.metadata_json.get('filename'),
            'file_type': chunk.metadata_json.get('file_type'),
            'chunk_index': chunk.chunk_index,
            'content': chunk.content,
            'score': 1.0,
            'splitter_name': chunk.metadata_json.get('splitter_name'),
            'parser_name': chunk.metadata_json.get('parser_name'),
            'section_type': chunk.metadata_json.get('section_type'),
            'section_title': chunk.metadata_json.get('section_title'),
            'page_number': chunk.page_number or chunk.metadata_json.get('page_number'),
            'source_path': chunk.metadata_json.get('source_path'),
            'start_offset': chunk.start_offset,
            'end_offset': chunk.end_offset,
            'retrieval_source': 'postgres_fallback',
            'retrieval_sources': ['postgres_fallback'],
        }
        for chunk in chunks
    ]
    logger.info(
        '[POSTGRES] fallback retrieval: query=%r hit_count=%s preview=%s',
        query,
        len(fallback_hits),
        _preview_hits(fallback_hits),
    )
    return fallback_hits


def validate_hits(db: Session, hits: list[dict], knowledge_base: str | None = None) -> list[dict]:
    """Discard deleted/disabled/stale index copies; return current authoritative text."""
    ids = [h.get('chunk_id') for h in hits if h.get('chunk_id')]
    if not ids:
        return []
    statement = select(Chunk, Document).join(Document).where(Chunk.id.in_(ids), Chunk.enabled.is_(True))
    if knowledge_base is not None:
        statement = statement.where(Document.knowledge_base == knowledge_base)
    rows = {c.id: (c, d) for c, d in db.execute(statement)}
    result = []
    for hit in hits:
        source = rows.get(hit.get('chunk_id'))
        if not source:
            continue
        chunk, document = source
        if hit.get('index_vector_id') is not None and str(hit['index_vector_id']) != str(chunk.vector_id):
            continue
        if hit.get('content') is not None and hit['content'] != chunk.content:
            continue
        metadata = chunk.metadata_json or {}
        result.append({**hit, 'content': chunk.content, 'document_id': document.id,
            'filename': document.filename, 'knowledge_base': document.knowledge_base,
            'chunk_index': chunk.chunk_index, 'page_number': chunk.page_number,
            **{k: metadata.get(k) for k in ('file_type', 'splitter_name', 'parser_name',
                'section_type', 'section_title', 'source_path', 'start_offset', 'end_offset')}})
    return result


def retrieve_chunks(db: Session, *, query: str, top_k: int = 5, retrieval_mode: str = 'hybrid',
                    knowledge_base: str | None = None, diagnostics: dict | None = None) -> list[dict]:
    """Retrieve current evidence; graph outages degrade explicitly to hybrid retrieval."""
    if retrieval_mode not in {'hybrid', 'graph_hybrid'}:
        raise ValueError('Unsupported retrieval mode')
    if retrieval_mode == 'graph_hybrid' and not knowledge_base:
        raise ValueError('Graph retrieval requires knowledge_base')
    if not 1 <= top_k <= 20:
        raise ValueError('top_k must be between 1 and 20')
    info = diagnostics if diagnostics is not None else {}
    info.update(graph_status='not_requested', degradation_reason=None)
    cleaned_query = query.strip()
    if not cleaned_query:
        return []
    candidate_k = _candidate_k(top_k)
    with ThreadPoolExecutor(max_workers=2) as executor:
        vector_future = executor.submit(_vector_retrieve_chunks, query=cleaned_query,
                                        candidate_k=candidate_k, knowledge_base=knowledge_base)
        bm25_future = executor.submit(_bm25_retrieve_chunks, query=cleaned_query,
                                      candidate_k=candidate_k, knowledge_base=knowledge_base)
        vector_hits = validate_hits(db, vector_future.result(), knowledge_base)
        bm25_hits = validate_hits(db, bm25_future.result(), knowledge_base)
    graph_hits = []
    if retrieval_mode == 'graph_hybrid':
        if not get_settings().graph_enabled:
            info.update(graph_status='disabled', degradation_reason='图谱功能未启用，已使用混合检索')
        else:
            try:
                from app.graph.retrieval import graph_candidates
                seeds = _fuse_hits(query=cleaned_query, vector_hits=vector_hits, bm25_hits=bm25_hits, top_k=10)
                graph_hits = validate_hits(db, graph_candidates(db, query=cleaned_query,
                    chunk_ids=[h['chunk_id'] for h in seeds], knowledge_base=knowledge_base), knowledge_base)
                info['graph_status'] = 'ok' if graph_hits else 'no_hits'
            except Exception as exc:
                logger.warning('Graph retrieval degraded: category=%s', type(exc).__name__)
                info.update(graph_status='degraded', degradation_reason='图谱暂不可用，已使用混合检索')
    hits = _fuse_hits(query=cleaned_query, vector_hits=vector_hits, bm25_hits=bm25_hits,
                      graph_hits=graph_hits, top_k=top_k)
    if hits:
        return hits
    return _postgres_fallback(db, query=cleaned_query, top_k=top_k, knowledge_base=knowledge_base)
