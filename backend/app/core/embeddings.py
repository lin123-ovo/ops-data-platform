from __future__ import annotations

import math
import time
from collections.abc import Mapping
from functools import lru_cache
from numbers import Real

from dashscope import TextEmbedding
from dashscope.common.error import DashScopeException, TimeoutException
from langchain_core.embeddings import Embeddings
from requests.exceptions import ConnectionError as RequestConnectionError
from requests.exceptions import Timeout as RequestTimeout

from app.config import Settings, get_settings
from app.core.vector_errors import (
    VectorError, VectorTimeoutError, VectorValidationError, vector_event, vector_operation,
)

_BATCH_SIZES = {'text-embedding-v1': 25, 'text-embedding-v2': 25, 'text-embedding-v3': 10, 'text-embedding-v4': 10}


def validate_vectors(vectors: object, *, expected_count: int, dimension: int) -> list[list[float]]:
    """Validate the full batch before any insert; do not normalize or truncate vectors."""
    started = time.perf_counter()
    try:
        if not isinstance(vectors, list) or len(vectors) != expected_count:
            raise VectorValidationError('向量数量与输入片段数量不一致')
        result: list[list[float]] = []
        for index, vector in enumerate(vectors):
            if not isinstance(vector, list) or len(vector) != dimension:
                raise VectorValidationError(f'第 {index + 1} 个向量维度不符合配置的 {dimension} 维')
            converted: list[float] = []
            for value in vector:
                if isinstance(value, bool) or not isinstance(value, Real):
                    raise VectorValidationError(f'第 {index + 1} 个向量包含非数值元素')
                try:
                    number = float(value)
                except (ValueError, OverflowError) as exc:
                    raise VectorValidationError('向量包含无法表示的数值') from exc
                if not math.isfinite(number) or abs(number) > 3.4028234663852886e38:
                    raise VectorValidationError(f'第 {index + 1} 个向量包含 NaN/Inf 或超出 Float32 范围的数值')
                converted.append(number)
            if not any(abs(value) >= 1.401298464324817e-45 for value in converted):
                raise VectorValidationError(f'第 {index + 1} 个向量为零向量')
            result.append(converted)
    except VectorValidationError as exc:
        vector_event('validation', started, status='failed', category='validation_error',
                     reason=str(exc), count=expected_count)
        raise
    vector_event('validation', started, count=len(result), dimension=dimension)
    return result


class ValidatedDashScopeEmbeddings(Embeddings):
    """DashScope adapter with bounded I/O, ordered output and safe diagnostics.

    Document retries are per batch and only for transient errors. No background
    worker continues after a timeout. The instance stores configuration, not results.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _embed(self, texts: list[str], *, text_type: str) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError('向量化输入不能为空')
        batch_size = _BATCH_SIZES.get(self.settings.embedding_model, 10)
        timeout = (
            self.settings.embedding_query_timeout_seconds if text_type == 'query'
            else self.settings.embedding_timeout_seconds
        )
        max_attempts = 1 if text_type == 'query' else self.settings.embedding_max_attempts
        vectors: list[list[float]] = []
        with vector_operation(model=self.settings.embedding_model, text_type=text_type):
            for offset in range(0, len(texts), batch_size):
                batch = texts[offset:offset + batch_size]
                with vector_operation(batch=offset // batch_size + 1):
                    for attempt in range(1, max_attempts + 1):
                        started = time.perf_counter()
                        transient = False
                        request_id = None
                        try:
                            response = TextEmbedding.call(
                                model=self.settings.embedding_model, input=batch, text_type=text_type,
                                api_key=self.settings.dashscope_api_key, request_timeout=timeout,
                            )
                            request_id = getattr(response, 'request_id', None)
                            status = getattr(response, 'status_code', None)
                            if status != 200:
                                transient = status == 429 or (isinstance(status, int) and 500 <= status < 600)
                                if status in (408, 504):
                                    transient = True
                                    raise VectorTimeoutError()
                                message = '向量服务请求失败，请检查服务状态或模型配置'
                                if status == 400 and any(
                                    word in str(getattr(response, 'message', '')).lower()
                                    for word in ('length', 'token', 'too long')
                                ):
                                    message = '向量输入超过模型长度限制，请缩短片段后重试'
                                raise VectorError(message, category=f'http_{status}')
                            vectors.extend(self._ordered_vectors(getattr(response, 'output', None), len(batch)))
                        except (RequestTimeout, TimeoutError, TimeoutException):
                            transient = True
                            error: VectorError = VectorTimeoutError()
                        except (RequestConnectionError, ConnectionError):
                            transient = True
                            error = VectorError('无法连接向量服务，请稍后重试', category='connection_error')
                        except VectorError as exc:
                            error = exc
                        except DashScopeException:
                            error = VectorError('向量服务请求失败，请检查输入或模型配置', category='sdk_error')
                        else:
                            vector_event('embedding', started, count=len(batch), attempt=attempt, request_id=request_id)
                            break
                        vector_event(
                            'embedding', started, status='failed', count=len(batch), attempt=attempt,
                            request_id=request_id, category=error.category, reason=str(error),
                        )
                        if not transient or attempt == max_attempts:
                            raise error
                        time.sleep(1)
        return vectors

    def _ordered_vectors(self, output: object, count: int) -> list[list[float]]:
        if not isinstance(output, Mapping):
            raise VectorValidationError()
        entries = output.get('embeddings')
        if not isinstance(entries, list) or len(entries) != count:
            raise VectorValidationError('向量数量与输入片段数量不一致')
        ordered: dict[int, object] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise VectorValidationError()
            index = entry.get('text_index')
            if type(index) is not int or not 0 <= index < count or index in ordered:
                raise VectorValidationError('向量返回索引缺失、重复或超出输入范围')
            ordered[index] = entry.get('embedding')
        return validate_vectors(
            [ordered[index] for index in range(count)], expected_count=count, dimension=self.settings.milvus_dimension,
        )

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, text_type='document')

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], text_type='query')[0]


@lru_cache(maxsize=1)
def get_embeddings() -> Embeddings:
    """Reuse the adapter for ingestion and queries without caching vector results."""
    return ValidatedDashScopeEmbeddings(get_settings())
