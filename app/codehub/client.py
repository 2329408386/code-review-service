"""CodeHub OpenAPI 客户端：行内评论、MR 整体评价、讨论置为已解决、查询当前用户。"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import Settings
from ..models import FindingRecord, ReviewTask

LOGGER = logging.getLogger(__name__)
HTTP_TIMEOUT_SECONDS = 20.0


class CodeHubError(RuntimeError):
    """kind：configuration / timeout / network / rate_limit / server_error / http_4xx。"""

    def __init__(self, message: str, *, kind: str, retryable: bool, status_code: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.status_code = status_code

    def __str__(self) -> str:
        return f"{self.kind}: {super().__str__()}"


class CodeHubClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)
        self._user_id: int | None = None
        self._user_loaded = False

    def close(self) -> None:
        self._http.close()

    @property
    def configured(self) -> bool:
        return bool(self.settings.codehub_private_token and self.settings.codehub_app_code)

    def _request(self, method: str, path: str, *, json: dict | None = None) -> httpx.Response:
        """统一的请求入口：注入认证头，把网络与 HTTP 错误转换为 CodeHubError。"""
        if not self.configured:
            raise CodeHubError("CodeHub credentials are not configured", kind="configuration", retryable=False)
        url = f"{self.settings.codehub_api_base_url.rstrip('/')}{path}"
        headers = {
            "private-token": self.settings.codehub_private_token,
            "X-Apig-AppCode": self.settings.codehub_app_code,
        }
        try:
            response = self._http.request(method, url, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise CodeHubError(f"{method} {path} timeout: {exc}", kind="timeout", retryable=True) from exc
        except httpx.RequestError as exc:
            raise CodeHubError(f"{method} {path} network error: {exc}", kind="network", retryable=True) from exc
        status = response.status_code
        if 200 <= status < 300:
            return response
        kind = "rate_limit" if status == 429 else "server_error" if status >= 500 else "http_4xx"
        raise CodeHubError(
            f"CodeHub HTTP {status}: {response.text[:500].replace(chr(10), ' ')}",
            kind=kind,
            retryable=status in (408, 429) or status >= 500,
            status_code=status,
        )

    def current_user_id(self) -> int | None:
        """评论 Token 所属用户（用于忽略服务自己发出的回复），只查询一次。"""
        if not self._user_loaded and self.configured:
            self._user_loaded = True
            try:
                user_id = self._request("GET", "/user").json().get("id")
                self._user_id = user_id if isinstance(user_id, int) and user_id > 0 else None
            except (CodeHubError, ValueError, AttributeError) as exc:
                LOGGER.warning("CodeHub current user lookup failed: %s", exc)
        return self._user_id

    def post_discussion(self, task: ReviewTask, finding: FindingRecord) -> str | None:
        """发送一条行内评论，返回 discussion ID。"""
        if finding.new_path == "/dev/null":
            # 删除行没有新侧：用旧行定位，路径用真实旧路径
            line_types = "old"
            position = {"new_path": finding.old_path, "old_path": finding.old_path, "old_line": finding.new_line}
        else:
            # 新增文件的 old_path 是 /dev/null，CodeHub 无法匹配，改用新路径
            line_types = "new"
            old_path = finding.new_path if finding.old_path == "/dev/null" else finding.old_path
            position = {"new_path": finding.new_path, "old_path": old_path, "new_line": finding.new_line}
        payload: dict[str, Any] = {
            "body": finding.comment_body(),
            "line_types": line_types,
            "position": {"position_type": "text", **position, "ignore_whitespace_change": True},
            "severity": finding.severity,
        }
        if task.assignee_id is not None:
            payload["assignee_id"] = task.assignee_id
        response = self._request(
            "POST", f"/projects/{task.project_id}/merge_requests/{task.mr_iid}/discussions", json=payload
        )
        try:
            discussion_id = response.json().get("id")
        except (ValueError, AttributeError):
            return None
        return str(discussion_id) if discussion_id else None

    def post_evaluation(self, task: ReviewTask) -> None:
        payload: dict[str, Any] = {"level": task.evaluation_level, "content": task.evaluation_content}
        if task.assignee_id is not None:
            payload["assignee_id"] = task.assignee_id
        self._request("POST", f"/projects/{task.project_id}/merge_requests/evaluations/{task.mr_iid}", json=payload)

    def resolve_discussion(self, project_id: int, mr_iid: int, discussion_id: str) -> None:
        self._request(
            "PUT",
            f"/projects/{project_id}/merge_requests/{mr_iid}/discussions/{discussion_id}",
            json={"resolved": True, "close_or_reopen_issue_when_discussion_resolved_or_unresolved": False},
        )
