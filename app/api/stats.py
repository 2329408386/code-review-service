"""健康检查与统计。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query

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


@router.get("/api/stats/tokens")
def token_stats(runtime: Runtime, limit: int | None = Query(default=None, ge=1, le=1000)) -> dict:
    return runtime.database.token_usage_summary(limit=limit)


@router.get("/api/stats/review-replies")
def review_reply_stats(runtime: Runtime) -> dict:
    return runtime.database.review_reply_stats()
