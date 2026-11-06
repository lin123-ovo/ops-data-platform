from __future__ import annotations

from dataclasses import dataclass

from app.rag.constants import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE, DEFAULT_SEPARATORS
from app.utils.text import clean_text


@dataclass(slots=True)
class SplitChunk:
    """切分结果结构。

    使用 dataclass 的好处是：
    - 字段含义清晰；
    - 后续无论是落库、向量化还是前端回显，都可以复用同一份数据结构。
    """

    chunk_index: int
    content: str
    # 坐标相对于统一换行、去除外侧空白行后的 section，而非原文件字节。
    start_offset: int
    end_offset: int


def _find_split_position(text: str, chunk_size: int, separators: list[str]) -> int:
    """在目标长度附近寻找最合适的截断点。

    逻辑优先级：
    1. 先尝试在 chunk_size 范围内从后往前找高优先级分隔符；
    2. 如果找不到，就直接在固定长度处截断，保证算法稳定。
    """

    if len(text) <= chunk_size:
        return len(text)

    candidate_text = text[:chunk_size]
    for separator in separators:
        split_index = candidate_text.rfind(separator)
        if split_index > 0:
            return split_index + len(separator)
    return chunk_size


def split_text_range(
    text: str,
    start: int,
    end: int,
    *,
    chunk_size: int,
    chunk_overlap: int,
    separators: list[str] | None = None,
) -> list[SplitChunk]:
    """切分已规范化文本的区间，正文始终直接取原始切片。"""

    if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError('chunk_size 必须大于 0，chunk_overlap 必须在 [0, chunk_size) 范围内')
    if not 0 <= start <= end <= len(text):
        raise ValueError('切分区间超出文本范围')
    chunks: list[SplitChunk] = []
    pending_start: int | None = None
    active_separators = DEFAULT_SEPARATORS if separators is None else separators
    while start < end:
        split_length = _find_split_position(text[start:min(end, start + chunk_size + 1)], chunk_size, active_separators)
        stop = min(end, start + split_length)
        content = text[start:stop]
        if content.strip():
            content_start = pending_start if pending_start is not None else start
            chunks.append(SplitChunk(len(chunks), text[content_start:stop], content_start, stop))
            pending_start = None
        elif chunks:
            # 不单独向量化空白块，也不丢掉代码/对齐文本内部的空白区间。
            previous = chunks[-1]
            previous.end_offset = max(previous.end_offset, stop)
            previous.content = text[previous.start_offset:previous.end_offset]
        elif pending_start is None:
            pending_start = start
        if stop == end:
            break
        # 短行可能比 overlap 更短；此时放弃重叠以保证向前推进。
        start = stop - chunk_overlap if stop - start > chunk_overlap else stop
    return chunks


def split_unstructured_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    separators: list[str] | None = None,
) -> list[SplitChunk]:
    """按最基础的非结构化文本策略切分内容。

    该实现故意保持简单可靠：
    - 足够适合第一版 RAG；
    - 不依赖额外复杂库；
    - 能保留 overlap 以降低跨 chunk 语义断裂问题。
    """

    cleaned_text = clean_text(text)
    if not cleaned_text:
        return []

    return split_text_range(
        cleaned_text, 0, len(cleaned_text), chunk_size=chunk_size,
        chunk_overlap=chunk_overlap, separators=separators,
    )
