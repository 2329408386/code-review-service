"""代码仓管理：隐藏代码仓与代码仓分组（只影响控制台展示与统计，不影响检视与回写）。

配置持久化在 runtime_settings 中，修改后立即生效。
- 隐藏的代码仓不进入统计，也不在检视记录中显示，但新的 MR 仍照常检视。
- 一个代码仓最多属于一个分组；统计可按分组汇总。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from ..storage import Database

LOGGER = logging.getLogger(__name__)
SETTING_KEY = "repository_catalog"
MAX_GROUP_NAME_LENGTH = 50
# 统计筛选中代表“未分组”的特殊组名
UNGROUPED = "__ungrouped__"


class RepositoryCatalogError(ValueError):
    pass


@dataclass(frozen=True)
class RepositoryGroup:
    name: str
    repositories: tuple[str, ...]

    def public(self) -> dict[str, object]:
        return {"name": self.name, "repositories": list(self.repositories)}


@dataclass(frozen=True)
class Catalog:
    hidden: tuple[str, ...] = ()
    groups: tuple[RepositoryGroup, ...] = ()

    def public(self) -> dict[str, object]:
        return {"hidden": list(self.hidden), "groups": [group.public() for group in self.groups]}


def _names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _normalize(payload: object, *, strict: bool) -> Catalog:
    """清洗配置：去空白、去重；组名不可为空 / 过长 / 重复，一个代码仓只能属于一个分组。

    strict=False 时跳过非法项（用于读取已存数据）。
    """
    data = payload if isinstance(payload, dict) else {}
    groups: list[RepositoryGroup] = []
    seen_groups: set[str] = set()
    owner: dict[str, str] = {}

    def fail(message: str) -> None:
        if strict:
            raise RepositoryCatalogError(message)

    for raw in data.get("groups") or []:
        name = str(raw.get("name") or "").strip() if isinstance(raw, dict) else ""
        if not name:
            fail("分组名称不能为空")
            continue
        if len(name) > MAX_GROUP_NAME_LENGTH:
            fail(f"分组名称不能超过 {MAX_GROUP_NAME_LENGTH} 个字符：{name}")
            continue
        if name == UNGROUPED:
            fail(f"分组名称不可用：{name}")
            continue
        if name in seen_groups:
            fail(f"分组名称重复：{name}")
            continue
        seen_groups.add(name)
        members: list[str] = []
        for repository in _names(raw.get("repositories")):
            if repository in owner:
                fail(f"代码仓 {repository} 同时属于分组 {owner[repository]} 和 {name}")
                continue
            owner[repository] = name
            members.append(repository)
        groups.append(RepositoryGroup(name, tuple(members)))
    return Catalog(hidden=tuple(_names(data.get("hidden"))), groups=tuple(groups))


class RepositoryCatalog:
    def __init__(self, database: Database):
        self.database = database
        self._lock = threading.Lock()
        try:
            stored = json.loads(database.get_setting(SETTING_KEY) or "{}")
        except json.JSONDecodeError:
            LOGGER.warning("stored repository catalog is invalid JSON, ignored")
            stored = {}
        self._catalog = _normalize(stored, strict=False)

    def configuration(self) -> dict[str, object]:
        with self._lock:
            return self._catalog.public()

    def replace(self, payload: dict) -> dict[str, object]:
        catalog = _normalize(payload, strict=True)
        with self._lock:
            self.database.set_setting(SETTING_KEY, json.dumps(catalog.public(), ensure_ascii=False))
            self._catalog = catalog
        LOGGER.info(
            "repository catalog updated hidden=%s groups=%s",
            list(catalog.hidden), [group.name for group in catalog.groups],
        )
        return self.configuration()

    def hidden(self) -> list[str]:
        with self._lock:
            return list(self._catalog.hidden)

    def groups(self) -> list[RepositoryGroup]:
        with self._lock:
            return list(self._catalog.groups)

    def group_of(self, repository: str) -> str | None:
        with self._lock:
            return next((g.name for g in self._catalog.groups if repository in g.repositories), None)

    def grouped_repositories(self) -> list[str]:
        with self._lock:
            return [repo for group in self._catalog.groups for repo in group.repositories]

    def repositories_in(self, group: str) -> list[str] | None:
        """分组内的代码仓；分组不存在时返回 None。"""
        with self._lock:
            found = next((g for g in self._catalog.groups if g.name == group), None)
            return list(found.repositories) if found else None
