"""用户对行内评论的回复：分类统计与自动置为已解决。"""
from __future__ import annotations

# 回复包含这些关键词时，服务自动把该讨论置为“已解决”
AUTO_RESOLVE_KEYWORDS = ("解决", "无风险", "确认", "没问题")


def classify_reply(note: str) -> str:
    """回复分类（按优先级）：没问题 > 确认/已解决 > 无风险 > 其它。"""
    text = note or ""
    if "没问题" in text:
        return "no_problem"
    if "确认" in text or "已解决" in text:
        return "resolved"
    if "无风险" in text:
        return "no_risk"
    return "other"


def should_auto_resolve(note: str) -> bool:
    return any(keyword in (note or "") for keyword in AUTO_RESOLVE_KEYWORDS)
