"""检视记录：列表、详情、过程事件（含 SSE 实时推送）、意见导出与管理员操作。"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from ..models import ReviewTask
from .deps import AdminOnly, Runtime

LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["reviews"])

PAGE_SIZES = (30, 60, 120, 240)
StatusFilter = Literal["pending", "running", "success", "failed", "ignored"]

_EXPORT_COLUMNS = (
    "任务ID", "代码仓", "MR编号", "MR标题", "作者", "意见ID", "意见状态", "严重级别", "文件路径",
    "旧文件路径", "行号", "标题", "内容", "创建时间", "回写时间", "回写错误", "重试次数",
)


def _get_task(runtime: Runtime, task_id: str) -> ReviewTask:
    task = runtime.database.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="review task not found")
    return task


def _csv_safe(value: str) -> str:
    """防止 Excel 把以 = + - @ 开头的单元格当作公式执行。"""
    return f"'{value}" if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


# ------------------------------------------------------------------ 查询


@router.get("/reviews")
def list_reviews(
    runtime: Runtime,
    repository: str | None = None,
    author: str | None = None,
    status: Annotated[list[StatusFilter] | None, Query()] = None,
    sort: Literal["asc", "desc"] = "desc",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1),
) -> dict:
    page_size = page_size if page_size in PAGE_SIZES else 30
    # 管理员隐藏的代码仓不在检视记录中显示（仍照常检视）
    filters = {"repository": repository, "author": author, "statuses": status, "exclude": runtime.catalog.hidden()}
    tasks = runtime.database.list_tasks(**filters, sort=sort, limit=page_size, offset=(page - 1) * page_size)
    summary = runtime.database.task_summary(**filters)
    total = summary["review_count"]
    return {
        "items": [task.model_dump(mode="json") for task in tasks],
        "total_count": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, -(-total // page_size)),
        "summary": summary,
    }


@router.get("/reviews/{task_id}")
def review_detail(task_id: str, runtime: Runtime) -> dict:
    task = _get_task(runtime, task_id)
    return {
        "task": task.model_dump(mode="json"),
        "findings": [finding.model_dump(mode="json") for finding in runtime.database.list_findings(task_id)],
        "last_event_id": runtime.database.last_event_id(task_id),
    }


@router.get("/reviews/{task_id}/events")
def review_events(
    task_id: str,
    runtime: Runtime,
    after_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    reasoning_only: bool = False,
) -> dict:
    _get_task(runtime, task_id)
    # 多取一条用于判断是否还有更多
    events = runtime.database.list_events([task_id], after_id=after_id, limit=limit + 1, reasoning_only=reasoning_only)
    page = events[:limit]
    return {
        "items": [event.model_dump(mode="json") for event in page],
        "last_id": page[-1].id if page else after_id,
        "has_more": len(events) > limit,
    }


@router.get("/reviews/{task_id}/findings/export")
def export_findings(task_id: str, runtime: Runtime) -> Response:
    task = _get_task(runtime, task_id)
    output = io.StringIO()
    output.write("﻿")  # BOM：让 Excel 以 UTF-8 打开
    writer = csv.writer(output)
    writer.writerow(_EXPORT_COLUMNS)
    for finding in runtime.database.list_findings(task_id):
        writer.writerow(
            [
                task.id, task.repository_name, task.mr_iid, _csv_safe(task.title), _csv_safe(task.author),
                finding.id, finding.status, finding.severity, _csv_safe(finding.display_path),
                _csv_safe(finding.old_path), finding.new_line, _csv_safe(finding.title), _csv_safe(finding.body),
                finding.created_at.isoformat(),
                finding.comment_sent_at.isoformat() if finding.comment_sent_at else "",
                finding.comment_error or "", finding.comment_retry_count,
            ]
        )
    ascii_id = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_" for ch in task_id)[:100]
    return Response(
        content=output.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="review-findings-{ascii_id}.csv"; '
            f"filename*=UTF-8''review-findings-{quote(task_id, safe='')}.csv"
        },
    )


@router.get("/review-events/stream")
async def event_stream(
    request: Request,
    runtime: Runtime,
    task_id: Annotated[list[str], Query()],
    after_id: int = Query(default=0, ge=0),
) -> StreamingResponse:
    """多个任务共用一条 SSE 连接，避免浏览器 HTTP/1.1 连接数耗尽。"""
    if not 1 <= len(task_id) <= 100:
        raise HTTPException(status_code=400, detail="one to 100 task_id values are required")
    header_id = request.headers.get("Last-Event-ID", "")
    cursor = max(after_id, int(header_id) if header_id.isdigit() else 0)

    async def generate():
        nonlocal cursor
        idle = 0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            events = await asyncio.to_thread(runtime.database.list_events, task_id, after_id=cursor, limit=200)
            for event in events:
                cursor = event.id
                data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                yield f"id: {event.id}\nevent: review_event\ndata: {data}\n\n"
            idle = 0 if events else idle + 1
            if idle >= 30:  # 约 15 秒无事件发送一次心跳
                idle = 0
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.get("/repositories")
def repositories(runtime: Runtime) -> list[dict]:
    return runtime.database.repositories(exclude=runtime.catalog.hidden())


@router.get("/authors")
def authors(runtime: Runtime) -> list[str]:
    return runtime.database.authors()


# ------------------------------------------------------------------ 管理员操作


@router.post("/tasks/{task_id}/rerun", dependencies=[AdminOnly], status_code=202)
def rerun(task_id: str, runtime: Runtime) -> dict:
    if _get_task(runtime, task_id).status in {"pending", "running"}:
        raise HTTPException(status_code=409, detail="任务正在排队或检视中")
    task = runtime.database.clone_task(task_id)
    runtime.database.append_event(task.id, stage="queue", status="pending", message="管理员触发重新检视，任务已进入检视队列")
    runtime.enqueue(task)
    LOGGER.info("[task=%s] admin rerun from task=%s", task.id, task_id)
    return {"accepted": True, "status": "pending", "task_id": task.id}


@router.post("/tasks/{task_id}/run-now", dependencies=[AdminOnly], status_code=202)
def run_now(task_id: str, runtime: Runtime) -> dict:
    _get_task(runtime, task_id)
    if not runtime.run_scheduled_now(task_id):
        raise HTTPException(status_code=409, detail="任务不在定时等待状态")
    runtime.database.append_event(task_id, stage="queue", status="pending", message="管理员已取消定时等待，任务立即执行")
    return {"accepted": True, "status": "pending", "task_id": task_id}


@router.delete("/tasks/{task_id}", dependencies=[AdminOnly])
def delete(task_id: str, runtime: Runtime) -> dict:
    if _get_task(runtime, task_id).status == "running":
        raise HTTPException(status_code=409, detail="任务正在检视中")
    runtime.database.delete_task(task_id)
    LOGGER.info("[task=%s] deleted by admin", task_id)
    return {"deleted": True, "task_id": task_id}
