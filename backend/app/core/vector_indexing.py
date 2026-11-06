"""Keep encoding context separate from source text and compensate acknowledged writes."""
from __future__ import annotations

from functools import wraps
from pathlib import Path
from time import perf_counter
from typing import Callable, Concatenate, ParamSpec, Protocol, TypeVar

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.embeddings import get_embeddings, validate_vectors
from app.core.milvus_client import MilvusVectorStore, normalize_vector_ids
from app.core.vector_errors import VectorError, vector_event, vector_operation

EMBEDDING_INPUT_VERSION = 'title_context_v1'
_PLACEHOLDERS = {'introduction', 'untitled'}
_NEW_VECTORS = 'vector_new_batches'
_OLD_VECTORS = 'vector_old_batches'


def build_embedding_text(text: str, metadata: dict[str, object]) -> str:
    """Add at most 256 characters of deterministic context without altering the body."""
    filename = str(metadata.get('filename') or '').strip()
    title = ' '.join(str(metadata.get('section_title') or '').split())
    name = ' '.join(Path(filename).stem.split()) if filename else ''
    parts = [f'文档：{name}'] if name else []
    if title and title.lower() not in _PLACEHOLDERS and title != name:
        parts.append(f'章节：{title}')
    prefix = '\n'.join(parts)[:254]  # Includes the two separator newlines in the 256-character budget.
    return f'{prefix}\n\n{text}' if prefix else text


def write_chunk_vectors(
    db: Session, store: MilvusVectorStore, *, texts: list[str], metadatas: list[dict],
) -> tuple[list[str | int], list[dict]]:
    """Encode enhanced text, store raw text and register known IDs for rollback cleanup."""
    settings = get_settings()
    if len(texts) != len(metadatas):
        raise VectorError('片段正文与元数据数量不一致')
    if not texts:
        return [], metadatas
    metadata = [
        {**{key: value for key, value in item.items() if key != 'vector_id'},
         'embedding_input_version': EMBEDDING_INPUT_VERSION,
         'embedding_model': settings.embedding_model, 'embedding_dimension': settings.milvus_dimension}
        for item in metadatas
    ]
    with vector_operation(
        document_id=metadata[0].get('document_id'), chunk_ids=[item.get('chunk_id') for item in metadata[:10]],
        chunk_count=len(metadata),
        model=settings.embedding_model,
    ):
        vectors = get_embeddings().embed_documents([
            build_embedding_text(text, item) for text, item in zip(texts, metadata, strict=True)
        ])
        vectors = validate_vectors(vectors, expected_count=len(texts), dimension=settings.milvus_dimension)
        ids = store.add_embeddings(texts=texts, embeddings=vectors, metadatas=metadata)
        if isinstance(ids, list) and ids:
            db.info.setdefault(_NEW_VECTORS, []).append((store, ids))
        if (not isinstance(ids, list) or len(ids) != len(texts)
                or any(type(value) not in (str, int) for value in ids) or len(set(ids)) != len(ids)):
            raise VectorError('向量写入返回的 ID 数量或唯一性异常，请检查入库状态')
    return ids, metadata


def defer_vector_delete(db: Session, store: MilvusVectorStore, ids: list[str | int]) -> None:
    """Retain old vectors until PostgreSQL commits the replacement."""
    if ids:
        db.info.setdefault(_OLD_VECTORS, []).append((store, normalize_vector_ids(ids)))


def _cleanup(batches: list[tuple[MilvusVectorStore, list[str | int]]], stage: str) -> None:
    for store, ids in batches:
        started = perf_counter()
        try:
            store.delete(ids=ids)
        except Exception as exc:
            # Compensation is best effort; do not mask the original database failure.
            vector_event(stage, started, status='failed', category=type(exc).__name__, count=len(ids))
        else:
            vector_event(stage, started, count=len(ids))


@event.listens_for(Session, 'after_commit')
def _vectors_after_commit(db: Session) -> None:
    db.info.pop(_NEW_VECTORS, None)
    _cleanup(db.info.pop(_OLD_VECTORS, []), 'milvus_old_cleanup')


@event.listens_for(Session, 'after_rollback')
def _vectors_after_rollback(db: Session) -> None:
    db.info.pop(_OLD_VECTORS, None)
    _cleanup(db.info.pop(_NEW_VECTORS, []), 'milvus_rollback_cleanup')


class _SessionOwner(Protocol):
    db: Session


_Owner = TypeVar('_Owner', bound=_SessionOwner)
_Params = ParamSpec('_Params')
_Result = TypeVar('_Result')


def vector_transaction(
    method: Callable[Concatenate[_Owner, _Params], _Result],
) -> Callable[Concatenate[_Owner, _Params], _Result]:
    """Rollback failed vector-backed service writes, including acknowledged external inserts."""
    @wraps(method)
    def wrapped(self: _Owner, *args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        with vector_operation():
            try:
                return method(self, *args, **kwargs)
            except Exception:
                self.db.rollback()
                raise
    return wrapped
