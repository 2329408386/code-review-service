"""本地提交检视：由 Git post-commit 钩子调用，检视“父提交..本次提交”的变更，结果只在平台展示。

为避免被用来读取服务器上的任意目录，只接受本机（127.0.0.1 / ::1）或已登录管理员的请求。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..review.git import GitError
from ..storage import ORIGIN_LOCAL
from .deps import Runtime, is_admin

LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api/local-reviews", tags=["local"])
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


class LocalReviewRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)  # 仓库内任意目录的绝对路径
    sha: str = Field(default="HEAD", min_length=1, max_length=200)


@router.post("")
def submit_local_review(payload: LocalReviewRequest, request: Request, runtime: Runtime) -> JSONResponse:
    host = request.client.host if request.client else ""
    if host not in LOOPBACK_HOSTS and not is_admin(request):
        raise HTTPException(status_code=403, detail="本地检视只接受本机或管理员的请求")
    try:
        commit = runtime.service.repositories.describe_local_commit(payload.path, payload.sha)
    except GitError as exc:
        raise HTTPException(status_code=422, detail=f"无法读取本地提交：{exc}") from exc
    if not commit.parent:
        return JSONResponse({"accepted": False, "status": "ignored", "reason": "根提交没有父提交，跳过检视"})

    task, created = runtime.database.create_task_if_not_duplicate(
        project_id=0,
        mr_iid=0,
        repository_url=commit.repository,
        repository_name=commit.name,
        source_branch=commit.branch,
        target_branch=commit.branch,
        source_sha=commit.sha,
        target_sha=commit.parent,
        action="commit",
        origin=ORIGIN_LOCAL,
        title=commit.subject,
        description=commit.body,
        author=commit.author,
    )
    if not created:
        return JSONResponse({"accepted": False, "status": "duplicate", "task_id": task.id, "review_status": task.status})
    runtime.database.append_event(task.id, stage="queue", status="pending", message="本地提交已进入检视队列")
    runtime.enqueue(task)
    LOGGER.info("[task=%s] local commit queued repo=%s sha=%s", task.id, commit.name, commit.sha[:12])
    return JSONResponse({"accepted": True, "status": "pending", "task_id": task.id}, status_code=202)
