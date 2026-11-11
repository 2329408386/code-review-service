"""MR 整体评价：根据意见数量与严重程度打 1-5 分，并生成 150 字以内的两点评价。"""
from __future__ import annotations

import re

from ..models import FindingRecord, ReviewTask

SEVERITY_PENALTY = {"major": 1.5, "minor": 0.8, "suggestion": 0.4}
MAX_CHARS = 150
_SECOND_POINT = re.compile(r"(?:2[.．、]\s*)?总体评价[：:]")


def rate_mr_quality(findings: list[FindingRecord]) -> int:
    """满分 5 分，每条意见按严重程度扣分，结果取整并限制在 1-5。"""
    score = 5.0 - sum(SEVERITY_PENALTY.get(finding.severity, 0.5) for finding in findings)
    return round(max(1.0, min(5.0, score)))


def _level_reason(level: int) -> str:
    return {
        5: "代码整体质量较高，未发现明显问题，可合并。",
        4: "代码整体质量较好，仅存在少量建议或一般问题，建议合并前酌情处理。",
        3: "代码整体可用，但存在若干需要关注的问题，建议修复后合并。",
        2: "代码存在较多问题，建议修复并复核后再合并。",
    }.get(level, "代码存在严重问题，强烈建议修复后重新检视。")


def _cap(text: str, limit: int = MAX_CHARS) -> str:
    """截断到 limit 字以内，保留换行分隔。"""
    if len(text) <= limit:
        return text
    result: list[str] = []
    used = 0
    for line in text.split("\n"):
        separator = 1 if result else 0
        room = limit - used - separator
        if room <= 0:
            break
        result.append(line[:room])
        used += separator + len(result[-1])
        if len(line) > room:
            break
    return "\n".join(result)


def _two_lines(text: str) -> str:
    """保证“检视思路”与“总体评价”分两行；模型合并成一行时在“总体评价”前断开。"""
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if len(lines) == 1 and (match := _SECOND_POINT.search(lines[0])) and match.start() > 0:
        lines = [lines[0][: match.start()].rstrip(), lines[0][match.start():]]
    return "\n".join(lines)


def fallback_content(task: ReviewTask, findings: list[FindingRecord]) -> str:
    """不依赖模型的模板评价。"""
    level = rate_mr_quality(findings)
    added = task.reviewed_added_line_count
    counts = {key: sum(1 for f in findings if f.severity == key) for key in ("major", "minor", "suggestion")}
    if not (task.reviewed_file_count or added):
        approach = "本次 MR 无新增代码变更，无需逐行检视"
        assessment = "未发现可审阅的代码变更，整体无风险。"
    else:
        if added:
            approach = f"按源码 diff 逐处核对，重点检查新增 {added} 行的逻辑与边界"
        else:
            approach = "按源码 diff 逐处核对，重点核查被删除代码的残留引用、调用与兼容性"
        assessment = (
            f"发现 {len(findings)} 条意见（严重{counts['major']}/一般{counts['minor']}/建议{counts['suggestion']}），"
            f"{_level_reason(level)}质量评分 {level}/5。"
        )
    return _cap(f"**检视思路**：{approach}\n**总体评价**：{assessment}")


def build_content(task: ReviewTask, findings: list[FindingRecord], llm_summary: str | None) -> str:
    """优先使用模型生成的摘要，失败或为空时使用模板。"""
    summary = _two_lines(llm_summary or "")
    return _cap(summary) if summary else fallback_content(task, findings)
