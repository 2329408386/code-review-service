"""CodeHub Webhook：MR 事件触发检视；评论回复事件用于统计与自动解决讨论。"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..codehub.client import CodeHubError
from ..codehub.replies import classify_reply, should_auto_resolve
from ..models import MergeRequestWebhook, NoteWebhook
from ..review.git import GitError, repository_name_from_url, validate_repository_url
from ..review.scope import skip_reason
from ..storage import derive_mr_url
from .deps import Runtime, get_runtime

LOGGER = logging.getLogger(__name__)


def verify_secret(request: Request) -> None:
    """配置了 CODEHUB_WEBHOOK_SECRET 时校验请求头 X-CodeHub-Token。"""
    expected = get_runtime(request).settings.codehub_webhook_secret
    if expected and not secrets.compare_digest(request.headers.get("X-CodeHub-Token", ""), expected):
        LOGGER.warning("webhook rejected: invalid shared secret")
        raise HTTPException(status_code=401, detail="invalid webhook credential")


router = APIRouter(prefix="/api/webhooks", tags=["webhooks"], dependencies=[Depends(verify_secret)])


def _old_revision(value: str | None) -> str | None:
    """全 0 的 oldrev 表示没有上一版本，按全量检视处理。"""
    value = (value or "").strip()
    return None if not value or set(value) == {"0"} else value


@router.post("/codehub")
def merge_request_event(payload: MergeRequestWebhook, runtime: Runtime) -> JSONResponse:
    attrs = payload.object_attributes
    repository_url = attrs.source.git_ssh_url
    repository = repository_name_from_url(repository_url)
    fields = {
        "project_id": payload.project.id,
        "mr_iid": attrs.iid,
        "repository_url": repository_url,
        "repository_name": repository,
        "target_repository_url": attrs.target.git_ssh_url if attrs.target else None,
        "source_branch": attrs.source_branch,
        "target_branch": attrs.target_branch,
        "source_sha": attrs.last_commit.id,
        "old_source_sha": _old_revision(attrs.oldrev),
        "target_sha": attrs.target_branch_commit.id if attrs.target_branch_commit else None,
        "action": attrs.action,
        "title": attrs.title,
        "description": attrs.description,
        "author": attrs.author.name,
        "assignee_id": payload.assignee_id(),
        "mr_url": attrs.last_commit.url or derive_mr_url(repository_url, attrs.last_commit.id),
    }
    LOGGER.info(
        "webhook project=%s mr=%s action=%s state=%s source=%s target=%s",
        payload.project.id, attrs.iid, attrs.action, attrs.state, attrs.source_branch, attrs.target_branch,
    )

    # 不需要检视的事件也落库为 ignored，便于在控制台追溯
    if reason := skip_reason(attrs, repository, runtime.settings, runtime.scope):
        task = runtime.database.create_task(**fields, status="ignored", ignored_reason=reason)
        LOGGER.info("[task=%s] webhook ignored: %s", task.id, reason)
        return JSONResponse({"accepted": False, "status": "ignored", "reason": reason, "task_id": task.id})

    try:
        validate_repository_url(repository_url, allowed_hosts=runtime.settings.repository_hosts())
    except GitError as exc:
        LOGGER.warning("webhook rejected project=%s mr=%s: %s", payload.project.id, attrs.iid, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task, created = runtime.database.create_task_if_not_duplicate(**fields)
    if not created:
        return JSONResponse(
            {"accepted": False, "status": "duplicate", "task_id": task.id, "review_status": task.status}
        )
    runtime.database.append_event(task.id, stage="queue", status="pending", message="Webhook 校验通过，任务已进入检视队列")
    runtime.enqueue(task)
    LOGGER.info("[task=%s] webhook accepted queue_size=%s", task.id, runtime.queue_size())
    return JSONResponse({"accepted": True, "status": "pending", "task_id": task.id}, status_code=202)


@router.post("/codehub-note")
def note_event(payload: NoteWebhook, runtime: Runtime) -> JSONResponse:
    project_id = payload.project.id if payload.project else payload.project_id
    attrs = payload.object_attributes
    if not project_id:
        raise HTTPException(status_code=400, detail="project id is required")
    if not attrs.discussion_id:
        raise HTTPException(status_code=400, detail="object_attributes.discussion_id is required")
    mr_iid = payload.merge_request.iid

    def ignored(reason: str) -> JSONResponse:
        return JSONResponse({"accepted": True, "action": "ignored", "reason": reason})

    if attrs.system:
        return ignored("system note")
    # 该 MR 所属代码仓决定使用哪套 CodeHub 凭据（分组可配置独立凭据）
    repository = runtime.database.repository_for_mr(project_id, mr_iid)
    if attrs.author_id is not None and attrs.author_id == runtime.codehub.current_user_id(repository):
        return ignored("service note")
    recorded = runtime.database.record_review_reply(
        project_id=project_id,
        mr_iid=mr_iid,
        discussion_id=attrs.discussion_id,
        author_id=attrs.author_id,
        note_id=attrs.id,
        note=attrs.note,
        category=classify_reply(attrs.note),
    )
    if not recorded:
        return ignored("duplicate note")
    if not should_auto_resolve(attrs.note):
        return ignored("no auto-resolve keyword")
    try:
        runtime.codehub.resolve_discussion(project_id, mr_iid, attrs.discussion_id, repository)
    except CodeHubError as exc:
        LOGGER.warning("[mr=%s][discussion=%s] auto-resolve failed: %s", mr_iid, attrs.discussion_id, exc)
        return JSONResponse(
            {"accepted": False, "action": "failed", "detail": str(exc)[:500]}, status_code=502
        )
    LOGGER.info("[mr=%s][discussion=%s] discussion auto-resolved", mr_iid, attrs.discussion_id)
    return JSONResponse({"accepted": True, "action": "resolved", "discussion_id": attrs.discussion_id})
