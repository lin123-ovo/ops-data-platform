"""Safe public errors and context shared by vector generation and storage."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter
from typing import Iterator
from uuid import uuid4

logger = logging.getLogger(__name__)
_context: ContextVar[dict[str, object] | None] = ContextVar('vector_context', default=None)


class VectorError(Exception):
    """Vector operation failure with a safe HTTP message and machine-readable category."""

    status_code = 502

    def __init__(self, message: str, *, category: str = 'upstream_error') -> None:
        super().__init__(message)
        self.category = category


class VectorValidationError(VectorError):
    """The provider returned an invalid or misaligned vector batch."""

    def __init__(self, message: str = '向量服务返回的数据校验失败，请重试或检查模型配置') -> None:
        super().__init__(message, category='validation_error')


class VectorTimeoutError(VectorError):
    status_code = 504

    def __init__(self, *, write: bool = False) -> None:
        message = '向量写入超时，写入结果未知，请刷新确认状态，避免重复提交' if write else '向量服务调用超时，请稍后重试'
        super().__init__(message, category='write_outcome_unknown' if write else 'timeout')


class VectorConfigurationError(VectorError):
    status_code = 500

    def __init__(self, message: str) -> None:
        super().__init__(message, category='configuration_error')


@contextmanager
def vector_operation(**fields: object) -> Iterator[None]:
    """Carry identifiers across nested calls without logging input text or credentials."""
    current = _context.get() or {'operation_id': uuid4().hex}
    token = _context.set({**current, **fields})
    try:
        yield
    finally:
        _context.reset(token)


def vector_event(stage: str, started_at: float, *, status: str = 'success', **fields: object) -> None:
    """Log only caller-selected identifiers, counters, categories and elapsed time."""
    event = {
        **(_context.get() or {}), 'stage': stage, 'status': status,
        'duration_ms': round((perf_counter() - started_at) * 1000, 2), **fields,
    }
    logger.log(logging.INFO if status == 'success' else logging.WARNING, '[VECTOR_OP] %s', event)
