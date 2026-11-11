"""路由共用依赖：获取运行时、管理员令牌校验。"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from ..runtime import ReviewRuntime


class AdminTokens:
    """内存中的管理员登录令牌（服务重启后需重新登录）。"""

    def __init__(self, ttl_seconds: int = 12 * 3600):
        self.ttl_seconds = ttl_seconds
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self) -> tuple[str, float]:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + self.ttl_seconds
        with self._lock:
            self._tokens[token] = expires_at
        return token, expires_at

    def valid(self, token: str) -> bool:
        with self._lock:
            expires_at = self._tokens.get(token)
            if expires_at is not None and expires_at < time.time():
                del self._tokens[token]
                expires_at = None
        return expires_at is not None

    def revoke(self, token: str) -> None:
        with self._lock:
            self._tokens.pop(token, None)


def bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    return authorization.removeprefix("Bearer ").strip() if authorization.startswith("Bearer ") else ""


def is_admin(request: Request) -> bool:
    token = bearer_token(request)
    return bool(token) and request.app.state.admin_tokens.valid(token)


def get_runtime(request: Request) -> ReviewRuntime:
    return request.app.state.runtime


def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(status_code=401, detail="需要管理员登录")


Runtime = Annotated[ReviewRuntime, Depends(get_runtime)]
AdminOnly = Depends(require_admin)
