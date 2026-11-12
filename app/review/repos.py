"""代码仓管理：隐藏代码仓与代码仓分组（只影响控制台展示与统计，不影响检视与回写）。

配置持久化在 runtime_settings 中，修改后立即生效。
- 隐藏的代码仓不进入统计，也不在检视记录中显示，但新的 MR 仍照常检视。
- 一个代码仓最多属于一个分组；统计可按分组汇总。
- 分组可配置独立的 CodeHub 凭据（private-token + AppCode）：组内代码仓的评论、评价与自动解决
  使用该凭据发送；未配置时使用 .env 的默认凭据。凭据只保存在服务端，接口不回显。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from ..codehub.client import Credentials
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
    codehub: Credentials = Credentials()

    def stored(self) -> dict[str, object]:
        """持久化形式（含凭据）。"""
        return {
            "name": self.name,
            "repositories": list(self.repositories),
            "codehub_private_token": self.codehub.private_token,
            "codehub_app_code": self.codehub.app_code,
        }

    def public(self) -> dict[str, object]:
        """接口返回形式：凭据只返回是否已配置。"""
        return {
            "name": self.name,
            "repositories": list(self.repositories),
            "codehub_configured": self.codehub.configured,
        }


@dataclass(frozen=True)
class Catalog:
    hidden: tuple[str, ...] = ()
    groups: tuple[RepositoryGroup, ...] = ()

    def stored(self) -> dict[str, object]:
        return {"hidden": list(self.hidden), "groups": [group.stored() for group in self.groups]}

    def public(self) -> dict[str, object]:
        return {"hidden": list(self.hidden), "groups": [group.public() for group in self.groups]}


def _names(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _text(value: object) -> str:
    return str(value or "").strip()


def _credentials(raw: dict, previous: Credentials, *, strict: bool, name: str) -> Credentials:
    """合并分组凭据：留空表示保持原值，clear_codehub 表示改回默认凭据；Token 与 AppCode 必须成对配置。"""
    if raw.get("clear_codehub"):
        return Credentials()
    credentials = Credentials(
        _text(raw.get("codehub_private_token")) or previous.private_token,
        _text(raw.get("codehub_app_code")) or previous.app_code,
    )
    if bool(credentials.private_token) != bool(credentials.app_code):
        if strict:
            raise RepositoryCatalogError(f"分组 {name} 的 CodeHub Token 与 AppCode 需同时配置")
        return Credentials()
    return credentials


def _normalize(payload: object, *, strict: bool, previous: Catalog | None = None) -> Catalog:
    """清洗配置：去空白、去重；组名不可为空 / 过长 / 重复，一个代码仓只能属于一个分组。

    strict=False 时跳过非法项（用于读取已存数据）。previous 用于保留未修改的分组凭据
    （按 previous_name 匹配，便于重命名后仍保留凭据）。
    """
    old_groups = {group.name: group for group in (previous.groups if previous else ())}
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
        old = old_groups.get(_text(raw.get("previous_name")) or name)
        credentials = _credentials(raw, old.codehub if old else Credentials(), strict=strict, name=name)
        groups.append(RepositoryGroup(name, tuple(members), credentials))
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
        with self._lock:
            catalog = _normalize(payload, strict=True, previous=self._catalog)
            self.database.set_setting(SETTING_KEY, json.dumps(catalog.stored(), ensure_ascii=False))
            self._catalog = catalog
        LOGGER.info(
            "repository catalog updated hidden=%s groups=%s",
            list(catalog.hidden), [group.name for group in catalog.groups],
        )
        return self.configuration()

    def codehub_credentials(self, repository: str) -> Credentials | None:
        """代码仓所在分组的独立 CodeHub 凭据；未分组或分组未配置时返回 None。"""
        with self._lock:
            group = next((g for g in self._catalog.groups if repository in g.repositories), None)
            return group.codehub if group and group.codehub.configured else None

    def has_codehub_credentials(self) -> bool:
        with self._lock:
            return any(group.codehub.configured for group in self._catalog.groups)

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
