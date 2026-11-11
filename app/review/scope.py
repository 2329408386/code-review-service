"""检视范围：决定一个 MR 事件是否需要检视。

优先级：MR 状态 / 事件类型 → 全局跳过的源分支（SKIP_SOURCE_BRANCHES）
→ 仓库目标分支白名单（控制台配置；未配置白名单的仓库检视所有目标分支）。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from ..config import Settings
from ..models import MergeRequestAttributes
from ..storage import Database

LOGGER = logging.getLogger(__name__)
SETTING_KEY = "review_target_branch_rules"


class ReviewScopeError(ValueError):
    pass


@dataclass(frozen=True)
class ScopeRule:
    """该仓库只检视合入列出目标分支的 MR。"""

    repository: str
    target_branches: tuple[str, ...]

    def public(self) -> dict[str, object]:
        return {"repository": self.repository, "target_branches": list(self.target_branches)}


def _normalize(rules: list[dict], *, strict: bool) -> tuple[ScopeRule, ...]:
    """清洗规则：去空白、分支去重、仓库不可重复。strict=False 时跳过非法项（用于读取已存数据）。"""
    result: list[ScopeRule] = []
    seen: set[str] = set()
    for rule in rules:
        repository = str(rule.get("repository") or "").strip() if isinstance(rule, dict) else ""
        branches = rule.get("target_branches") if isinstance(rule, dict) else None
        cleaned = list(dict.fromkeys(str(b).strip() for b in branches or [] if str(b).strip()))
        if not repository or not cleaned:
            if strict:
                raise ReviewScopeError("代码仓名称与目标分支均不能为空")
            continue
        if repository in seen:
            if strict:
                raise ReviewScopeError(f"代码仓重复：{repository}")
            continue
        seen.add(repository)
        result.append(ScopeRule(repository, tuple(cleaned)))
    return tuple(result)


class ReviewScopeRegistry:
    """仓库目标分支白名单，持久化在 runtime_settings 中，修改后立即生效。"""

    def __init__(self, database: Database):
        self.database = database
        self._lock = threading.Lock()
        try:
            stored = json.loads(database.get_setting(SETTING_KEY) or "[]")
        except json.JSONDecodeError:
            LOGGER.warning("stored review scope rules are invalid JSON, ignored")
            stored = []
        self._rules = _normalize(stored if isinstance(stored, list) else [], strict=False)

    def rules(self) -> list[dict[str, object]]:
        with self._lock:
            return [rule.public() for rule in self._rules]

    def replace(self, rules: list[dict]) -> list[dict[str, object]]:
        values = _normalize(rules, strict=True)
        encoded = json.dumps([rule.public() for rule in values], ensure_ascii=False)
        with self._lock:
            self.database.set_setting(SETTING_KEY, encoded)
            self._rules = values
        LOGGER.info("review scope updated rules=%s", [rule.repository for rule in values])
        return self.rules()

    def allowed_branches(self, repository: str) -> tuple[str, ...] | None:
        """返回仓库的白名单；未配置时返回 None。"""
        with self._lock:
            return next((rule.target_branches for rule in self._rules if rule.repository == repository), None)


def skip_reason(
    attrs: MergeRequestAttributes, repository: str, settings: Settings, scope: ReviewScopeRegistry
) -> str | None:
    """返回 MR 不需要检视的原因；需要检视时返回 None。"""
    if attrs.state != "opened":
        return f"state is {attrs.state!r}, expected 'opened'"
    if attrs.work_in_progress:
        return "merge request is work in progress"
    if attrs.action not in {"open", "update"}:
        return f"action {attrs.action!r} is not supported"
    if attrs.source_branch in settings.skipped_source_branches():
        return f"源分支 {attrs.source_branch} 命中全局跳过列表，跳过代码检视"
    allowed = scope.allowed_branches(repository)
    if allowed is not None:
        if attrs.target_branch in allowed:
            return None
        return (
            f"{repository} 仅检视目标分支 {'、'.join(allowed)}，"
            f"目标分支 {attrs.target_branch} 不在允许列表，跳过代码检视"
        )
    return None
