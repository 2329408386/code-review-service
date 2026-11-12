"""健康检查与统计。"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query

from ..review.repos import UNGROUPED
from .deps import Runtime

LOGGER = logging.getLogger(__name__)
router = APIRouter(tags=["stats"])


@router.get("/health")
def health(runtime: Runtime) -> dict:
    try:
        db_ok = runtime.database.is_healthy()
    except Exception:
        LOGGER.exception("health check: database unavailable")
        db_ok = False
    route = runtime.registry.route_for("")
    return {
        "status": "ok" if db_ok else "degraded",
        "db_ok": db_ok,
        "queue_size": runtime.queue_size(),
        "active_reviews": runtime.active_reviews(),
        "active_codex_runs": runtime.codex.active_runs(),
        "publish_queue_size": runtime.publisher.size(),
        "publisher_alive": runtime.publisher.is_alive(),
        "codex_installed": runtime.codex.available,
        "codex_version": runtime.codex.version,
        "codegraph_enabled": runtime.codegraph.enabled,
        "codegraph_installed": runtime.codegraph.installed,
        "codehub_configured": runtime.codehub.configured,
        "configured_models": runtime.registry.model_names(),
        "default_model": route.name if route else None,
    }


STATS_RANGES = {"7d": 7, "30d": 30, "90d": 90, "all": None}
STATS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _summarize(name: str, rows: list[dict]) -> dict:
    """把若干代码仓的统计行合并为一个分组的汇总。"""
    keys = ("review_count", "finding_count", "input_tokens", "output_tokens", "cached_input_tokens", "total_tokens")
    item = {"name": name, "repository_count": len(rows)} | {key: sum(row[key] for row in rows) for key in keys}
    item["replies"] = {
        key: sum(row["replies"].get(key, 0) for row in rows)
        for key in ("resolved", "no_risk", "no_problem", "other", "finding_count", "classification_total")
    }
    return item


@router.get("/api/stats/overview")
def stats_overview(
    runtime: Runtime,
    range_: Literal["7d", "30d", "90d", "all"] = Query(default="30d", alias="range"),
    group: str | None = None,
    repository: str | None = None,
) -> dict:
    """统计总览。维度：全部 / 某分组（group，__ungrouped__ 表示未分组）/ 某代码仓（repository）。

    管理员隐藏的代码仓始终不纳入统计。
    """
    catalog = runtime.catalog
    exclude = catalog.hidden()
    include: list[str] | None = None
    if repository:
        include = [repository]
    elif group == UNGROUPED:
        exclude = exclude + catalog.grouped_repositories()
    elif group:
        include = catalog.repositories_in(group)
        if include is None:
            raise HTTPException(status_code=404, detail=f"分组不存在：{group}")

    days = STATS_RANGES[range_]
    today = datetime.now(STATS_TIMEZONE).replace(hour=0, minute=0, second=0, microsecond=0)
    since = (today - timedelta(days=days - 1)) if days else None
    data = runtime.database.stats_overview(
        since=since.astimezone(UTC).isoformat() if since else None, include=include, exclude=exclude
    )

    owner = {repo: g.name for g in catalog.groups() for repo in g.repositories}
    for row in data["repositories"]:
        row["group"] = owner.get(row["name"])
    groups = []
    for item in catalog.groups():
        rows = [row for row in data["repositories"] if row["group"] == item.name]
        groups.append(_summarize(item.name, rows))
    ungrouped = [row for row in data["repositories"] if row["group"] is None]
    if ungrouped and catalog.groups():
        groups.append(_summarize("未分组", ungrouped) | {"key": UNGROUPED})
    data["groups"] = groups
    first_day = data["trend"][0]["day"] if data["trend"] else today.date().isoformat()
    data["range"] = {
        "key": range_,
        "since_day": since.date().isoformat() if since else first_day,
        "today": today.date().isoformat(),
    }
    return data


@router.get("/api/stats/review-replies")
def review_reply_stats(runtime: Runtime) -> dict:
    return runtime.database.review_reply_stats(exclude=runtime.catalog.hidden())
