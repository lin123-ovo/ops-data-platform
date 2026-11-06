from __future__ import annotations

import json
from functools import lru_cache
from time import perf_counter
from typing import Callable, TypeVar

import grpc
from langchain_core.documents import Document
from pymilvus import DataType, MilvusClient
from pymilvus.exceptions import MilvusException

from app.config import get_settings
from app.core.embeddings import get_embeddings, validate_vectors
from app.core.vector_errors import (
    VectorConfigurationError, VectorError, VectorTimeoutError, vector_event, vector_operation,
)

_Result = TypeVar('_Result')


class BoundedMilvusClient(MilvusClient):
    """Forward retry controls that PyMilvus 2.5's high-level insert discards."""

    def insert(
        self, collection_name: str, data: list[dict], timeout: float | None = None, **kwargs: object,
    ) -> dict[str, object]:
        connection = self._get_connection()
        schema = connection.describe_collection(collection_name, timeout=timeout, **kwargs)
        result = connection.insert_rows(
            collection_name, data, partition_name='', schema=schema, timeout=timeout, **kwargs,
        )
        return {'ids': list(result.primary_keys), 'insert_count': result.insert_count}


def normalize_vector_ids(vector_ids: list[str | int]) -> list[str | int]:
    """Restore numeric Milvus primary keys loaded from string database columns."""
    return [int(value) if isinstance(value, str) and value.isdecimal() else value for value in vector_ids]


def _milvus_call(stage: str, operation: Callable[[], _Result], *, write: bool = False) -> _Result:
    started = perf_counter()
    try:
        result = operation()
    except (MilvusException, grpc.RpcError, grpc.FutureTimeoutError, TimeoutError, ConnectionError) as exc:
        timeout = isinstance(exc, (TimeoutError, grpc.FutureTimeoutError))
        if isinstance(exc, grpc.RpcError):
            timeout = timeout or exc.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        # PyMilvus sometimes wraps the gRPC deadline in a MilvusException.
        timeout = timeout or any(word in str(exc).lower() for word in ('timeout', 'timed out', 'deadline'))
        error = VectorTimeoutError(write=write) if timeout else VectorError('向量数据库调用失败，请稍后重试')
        vector_event(stage, started, status='failed', category=error.category,
                     exception_type=type(exc).__name__, attempt=1)
        raise error from exc
    vector_event(stage, started, attempt=1)
    return result


@lru_cache(maxsize=1)
def get_milvus_client() -> MilvusClient:
    """Reuse the native client, with an actual connection timeout."""
    settings = get_settings()
    with vector_operation():
        return _milvus_call('milvus_connect', lambda: BoundedMilvusClient(
            uri=settings.resolved_milvus_uri, timeout=settings.milvus_timeout_seconds,
        ))


class MilvusVectorStore:
    """Small synchronous facade preserving the RAG add/search/delete contracts.

    Explicit native calls keep every RPC bounded and avoid the LangChain wrapper's
    payload-dumping error handler. Application retries never repeat an insert.
    """

    def __init__(self, collection_name: str) -> None:
        self.settings = get_settings()
        self.collection_name = collection_name
        self.embedding_function = get_embeddings()
        self.client = get_milvus_client()

    @property
    def _rpc(self) -> dict[str, object]:
        # In this PyMilvus version, a float timeout plus retry_times=0 prevents
        # its retry decorator from replaying ambiguous writes (including 429).
        return {'timeout': float(self.settings.milvus_timeout_seconds), 'retry_times': 0, 'retry_on_rate_limit': False}

    def _exists(self) -> bool:
        return _milvus_call('milvus_schema', lambda: self.client.has_collection(self.collection_name, **self._rpc))

    def _check_schema(self) -> None:
        started = perf_counter()
        schema = _milvus_call('milvus_schema', lambda: self.client.describe_collection(
            self.collection_name, **self._rpc,
        ))
        vector = next((field for field in schema.get('fields', []) if field.get('name') == 'vector'), None)
        try:
            dimension = int((vector.get('params') or {}).get('dim', 0)) if vector else 0
        except (TypeError, ValueError):
            dimension = 0
        if dimension != self.settings.milvus_dimension:
            vector_event('collection_validation', started, status='failed', category='dimension_mismatch')
            raise VectorConfigurationError('Milvus 集合维度与模型配置不一致，请检查配置；现有集合未修改')
        if vector.get('type') != DataType.FLOAT_VECTOR:
            raise VectorConfigurationError('Milvus 集合向量字段类型不符合 Float32 配置')

    def _ensure_collection(self) -> None:
        if self._exists():
            self._check_schema()
            self._ensure_index()
            return
        schema = self.client.create_schema(auto_id=True, enable_dynamic_field=True)
        schema.add_field('text', DataType.VARCHAR, max_length=65535)
        schema.add_field('pk', DataType.INT64, is_primary=True, auto_id=True)
        schema.add_field('vector', DataType.FLOAT_VECTOR, dim=self.settings.milvus_dimension)
        _milvus_call('milvus_create', lambda: self.client.create_collection(
            self.collection_name, schema=schema, consistency_level='Session', **self._rpc,
        ))
        self._ensure_index()

    def _ensure_index(self) -> None:
        # A previous create may have succeeded before index creation failed.
        names = _milvus_call('milvus_index_state', lambda: self.client.list_indexes(
            self.collection_name, field_name='vector', **self._rpc,
        ))
        if names:
            return
        indexes = self.client.prepare_index_params()
        indexes.add_index(field_name='vector', index_type='AUTOINDEX', metric_type='COSINE')
        # Avoid the SDK's unbounded synchronous index-building poll for float timeouts.
        _milvus_call('milvus_index', lambda: self.client.create_index(
            self.collection_name, index_params=indexes, sync=False, **self._rpc,
        ))

    def add_embeddings(
        self, *, texts: list[str], embeddings: list[list[float]], metadatas: list[dict],
    ) -> list[str | int]:
        """Insert raw text and already validated vectors; never re-embed text."""
        if len(texts) != len(metadatas):
            raise VectorError('片段正文与元数据数量不一致')
        vectors = validate_vectors(embeddings, expected_count=len(texts), dimension=self.settings.milvus_dimension)
        if not texts:
            return []
        self._ensure_collection()
        ids: list[str | int] = []
        for offset in range(0, len(texts), 1000):
            rows = [
                {**metadatas[index], 'text': texts[index], 'vector': vectors[index]}
                for index in range(offset, min(offset + 1000, len(texts)))
            ]
            with vector_operation(batch=offset // 1000 + 1, count=len(rows)):
                try:
                    result = _milvus_call('milvus_insert', lambda: self.client.insert(
                        self.collection_name, data=rows, **self._rpc,
                    ), write=True)
                    returned = result.get('ids')
                    if not isinstance(returned, list):
                        raise VectorError('向量写入未返回有效 ID，写入结果未知，请刷新确认状态')
                    valid_ids = [value for value in returned if type(value) in (str, int)]
                    ids.extend(valid_ids)
                    if len(returned) != len(rows) or len(valid_ids) != len(returned):
                        vector_event('insert_validation', perf_counter(), status='failed',
                                     category='invalid_insert_ids', returned_count=len(returned))
                        raise VectorError('向量写入返回的 ID 数量或类型异常，请检查入库状态')
                except Exception:
                    # Preserve the original failure; only acknowledged IDs can be cleaned safely.
                    if ids:
                        self.cleanup(ids)
                    raise
        return ids

    def cleanup(self, ids: list[str | int]) -> None:
        """Best-effort compensation; a cleanup failure must remain visible."""
        try:
            self.delete(ids=ids)
        except Exception as exc:
            vector_event('milvus_compensation', perf_counter(), status='failed',
                         category=type(exc).__name__, count=len(ids))

    def delete(self, *, ids: list[str | int]) -> None:
        if ids:
            # A literal PK filter avoids the SDK's implicit schema lookup without retry controls.
            expression = f'pk in {json.dumps(normalize_vector_ids(ids))}'
            _milvus_call('milvus_delete', lambda: self.client.delete(
                self.collection_name, filter=expression, **self._rpc,
            ), write=True)

    def similarity_search_with_score(self, query: str, *, k: int, knowledge_base: str | None = None) -> list[tuple[Document, float]]:
        with vector_operation(model=self.settings.embedding_model):
            if not self._exists():
                return []
            self._check_schema()
            vector = self.embedding_function.embed_query(query)
            _milvus_call('milvus_load', lambda: self.client.load_collection(self.collection_name, **self._rpc))
            result = _milvus_call('milvus_search', lambda: self.client.search(
                self.collection_name, data=[vector], anns_field='vector', limit=k,
                search_params={'metric_type': 'COSINE', 'params': {}}, output_fields=['*'],
                filter=('knowledge_base == ' + json.dumps(knowledge_base, ensure_ascii=True)) if knowledge_base is not None else '',
                consistency_level='Session', **self._rpc,
            ))
            hits: list[tuple[Document, float]] = []
            for hit in result[0] if result else []:
                entity = dict(hit.get('entity') or {})
                text = entity.pop('text', '')
                entity.pop('vector', None)
                entity.setdefault('pk', hit.get('id'))
                hits.append((Document(page_content=text, metadata=entity), float(hit['distance'])))
            return hits


def get_vector_store(collection_name: str | None = None) -> MilvusVectorStore:
    """Create a facade without creating, rebuilding or loading any collection."""
    return MilvusVectorStore(collection_name or get_settings().milvus_collection)
