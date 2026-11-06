from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from app.utils.markdown import has_unclosed_fence

logger = logging.getLogger(__name__)
REPORT_PREFIX = 'quality_report_v1='


@dataclass(slots=True)
class QualityReport:
    checked: bool = False
    warnings: list[str] = field(default_factory=list)


def inspect_text_quality(text: str, *, markdown: bool = False) -> list[str]:
    """只报告可观察异常，不自动纠错或推断业务内容。"""

    warnings: list[str] = []
    replacement_count = text.count('\ufffd')
    control_count = sum(ord(char) < 32 and char not in '\n\r\t' for char in text)
    if replacement_count:
        warnings.append(f'发现 {replacement_count} 个替代字符（�），可能存在解码或识别问题，请核对原文。')
    if control_count:
        warnings.append(f'发现 {control_count} 个异常控制字符，请核对解析结果。')
    if markdown and has_unclosed_fence(text):
        warnings.append('Markdown 代码围栏未闭合，代码块结构可能不完整，请核对原文。')
    return warnings


def encode_quality_report(summary: str, report: QualityReport) -> str:
    """在解析说明后追加版本化报告，替换已有报告而不累积。"""

    description = '\n'.join(line for line in summary.splitlines() if not line.startswith(REPORT_PREFIX))
    payload = json.dumps({'checked': report.checked, 'warnings': report.warnings}, ensure_ascii=False)
    return f'{description}\n{REPORT_PREFIX}{payload}'


def decode_quality_report(summary: str | None) -> QualityReport:
    """读取报告；旧记录和损坏报告返回未检测，失败不影响文档查看。"""

    for line in reversed((summary or '').splitlines()):
        if not line.startswith(REPORT_PREFIX):
            continue
        try:
            payload = json.loads(line[len(REPORT_PREFIX):])
        except json.JSONDecodeError:
            logger.warning('Malformed document quality report')
            return QualityReport()
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get('checked'), bool)
            or not isinstance(payload.get('warnings'), list)
            or not all(isinstance(item, str) for item in payload['warnings'])
        ):
            logger.warning('Invalid document quality report structure')
            return QualityReport()
        return QualityReport(checked=payload['checked'], warnings=payload['warnings'])
    return QualityReport()
