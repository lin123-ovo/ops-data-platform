from __future__ import annotations

import re

from app.rag.constants import DEFAULT_CHUNK_OVERLAP, DEFAULT_CHUNK_SIZE
from app.rag.splitters.unstructured import SplitChunk, split_text_range
from app.utils.text import clean_text

_HTML_BLOCK = re.compile(
    r'<table\b[^>]*>.*?</table>|<div\b[^>]*>.*?<img\b[^>]*>.*?</div>',
    re.IGNORECASE | re.DOTALL,
)


def split_semi_structured_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[SplitChunk]:
    """按段落聚合并保护 HTML 表格；偏移始终指向规范化 section 的切片。"""

    cleaned_text = clean_text(text)
    if not cleaned_text:
        return []
    if chunk_size <= 0 or not 0 <= chunk_overlap < chunk_size:
        raise ValueError('chunk_size 必须大于 0，chunk_overlap 必须在 [0, chunk_size) 范围内')

    # 使用真实区间保护 HTML，避免占位符恢复后丢失偏移或缩进。
    protected = [match.span() for match in _HTML_BLOCK.finditer(cleaned_text) if len(match.group()) <= 8000]
    boundaries = [0]
    block_index = 0
    for match in re.finditer(r'\n[ \t]*\n(?:[ \t]*\n)*', cleaned_text):
        while block_index < len(protected) and protected[block_index][1] <= match.start():
            block_index += 1
        if block_index < len(protected) and protected[block_index][0] < match.end():
            continue
        boundaries.append(match.end())
    boundaries.append(len(cleaned_text))

    ranges: list[tuple[int, int]] = []
    start = boundaries[0]
    end = start
    for block_start, block_end in zip(boundaries, boundaries[1:]):
        if end > start and block_end - start > chunk_size:
            ranges.append((start, end))
            start = block_start
        end = block_end
    if end > start:
        ranges.append((start, end))

    chunks: list[SplitChunk] = []
    for start, end in ranges:
        contains_html = any(left < end and right > start for left, right in protected)
        if contains_html:
            candidates = [SplitChunk(0, cleaned_text[start:end], start, end)]
        else:
            candidates = split_text_range(
                cleaned_text, start, end, chunk_size=chunk_size, chunk_overlap=chunk_overlap,
            )
        for chunk in candidates:
            if chunk.content.strip():
                chunk.chunk_index = len(chunks)
                chunks.append(chunk)
    return chunks
