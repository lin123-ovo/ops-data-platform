from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Iterator

_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')


@dataclass(frozen=True, slots=True)
class MarkdownLine:
    text: str
    start: int
    in_fence: bool


def iter_markdown_lines(text: str) -> Iterator[MarkdownLine]:
    """遍历原始行和围栏状态；围栏边界行也视为围栏内部。

    支持反引号与波浪线，结束标记必须同字符且不短于开始标记。
    未闭合围栏保护到末尾。调用方负责先统一换行。
    """

    fence = ''
    offset = 0
    for line in text.splitlines(keepends=True):
        match = _FENCE.match(line.rstrip('\n'))
        protected = bool(fence)
        if match:
            marker, info = match.groups()
            if fence:
                if marker[0] == fence[0] and len(marker) >= len(fence) and not info.strip():
                    fence = ''
            elif marker[0] != '`' or '`' not in info:
                fence = marker
                protected = True
        yield MarkdownLine(text=line, start=offset, in_fence=protected)
        offset += len(line)


def has_unclosed_fence(text: str) -> bool:
    """判断最后一行之后是否仍在围栏内，不修改文本。"""

    fence = ''
    for line in text.split('\n'):
        match = _FENCE.match(line)
        if not match:
            continue
        marker, info = match.groups()
        if fence:
            if marker[0] == fence[0] and len(marker) >= len(fence) and not info.strip():
                fence = ''
        elif marker[0] != '`' or '`' not in info:
            fence = marker
    return bool(fence)
