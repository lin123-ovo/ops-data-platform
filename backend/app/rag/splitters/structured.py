from __future__ import annotations

import re

from app.rag.constants import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
from app.rag.splitters.unstructured import SplitChunk, split_text_range
from app.utils.text import clean_text
from app.utils.markdown import iter_markdown_lines

STRUCTURED_LINE_PATTERNS = (
    r'^\s*[\w.-]+\s*[:：]\s*.+$',
    r'^\s*[-*]\s+.+$',
    r'^\s*\d+[.)、]\s+.+$',
    r'^\s*(create|alter|drop|select|insert|update|delete)\b.+$',
)


def _is_structured_line(line: str) -> bool:
    """判断单行是否更像结构化/配置型内容。"""

    normalized_line = line.strip()
    if not normalized_line:
        return False
    return any(re.match(pattern, normalized_line, flags=re.IGNORECASE) for pattern in STRUCTURED_LINE_PATTERNS)


def split_structured_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[SplitChunk]:
    """面向结构化文本的切分策略。

    适用场景：
    - 参数说明
    - 字段定义
    - SQL / DDL
    - 配置项列表

    目标是尽量让一个 chunk 保留完整的“条目块”语义。
    """

    cleaned_text = clean_text(text)
    if not cleaned_text:
        return []

    # 只识别边界，保存时不剥离行首/行尾空白，也不重新拼接正文。
    boundaries = [0]
    after_blank = False
    for line in iter_markdown_lines(cleaned_text):
        is_blank = not line.text.strip()
        if not line.in_fence and not is_blank:
            if line.start > boundaries[-1] and (after_blank or _is_structured_line(line.text)):
                boundaries.append(line.start)
        after_blank = is_blank and not line.in_fence
    boundaries.append(len(cleaned_text))

    chunks: list[SplitChunk] = []
    for start, end in zip(boundaries, boundaries[1:]):
        for chunk in split_text_range(
            cleaned_text, start, end, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
        ):
            chunk.chunk_index = len(chunks)
            chunks.append(chunk)
    return chunks
