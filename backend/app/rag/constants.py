from __future__ import annotations

# 这里先采用最基础、最稳妥的一组默认切分参数。
# 后续接入前端调试台时，可以把这些参数暴露成可配置项。
# 这里按 Python 字符串长度计算，即字符数，不是模型 tokenizer 的 token 数。
# 500/50 是默认目标；半结构化策略为保护完整表格等结构，可能保留更长的块。
DEFAULT_CHUNK_SIZE = 500
DEFAULT_CHUNK_OVERLAP = 50

# 优先按“结构更强”的分隔符切分，这样能尽量保留语义完整性。
DEFAULT_SEPARATORS = [
    '\n\n',
    '\n',
    '。',
    '！',
    '？',
    '；',
    '，',
    ' ',
]
