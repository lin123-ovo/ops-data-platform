from __future__ import annotations

import math


def clean_text(text: str) -> str:
    """统一换行并移除外侧空白行，保留正文缩进、对齐和内部空行。

    返回新字符串；不修改行内或行尾空格，避免破坏代码、表格和 Markdown。
    """

    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return '\n'.join(lines[start:end])


def estimate_token_count(text: str) -> int:
    """粗略估算 token 数量。

    这里不引入额外 tokenizer 依赖，先使用一个足够稳定的近似值。
    对中文和英文混排场景，这个数字不追求完全准确，主要用于：
    - chunk 管理页展示；
    - 后续检索和切分调试。
    """

    cleaned_text = clean_text(text)
    if not cleaned_text:
        return 0
    return max(1, math.ceil(len(cleaned_text) / 4))
