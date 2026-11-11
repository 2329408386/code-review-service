"""管理员登录 / 登出 / 状态。"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, HTTPException, Request

from ..models import LoginRequest
from .deps import Runtime, bearer_token, is_admin

LOGGER = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(credentials: LoginRequest, request: Request, runtime: Runtime) -> dict:
    settings = runtime.settings
    if not settings.admin_enabled:
        raise HTTPException(status_code=403, detail="未启用管理员登录")
    valid = secrets.compare_digest(credentials.username, settings.admin_username) & secrets.compare_digest(
        credentials.password, settings.admin_password
    )
    if not valid:
        LOGGER.warning("admin login rejected username=%r", credentials.username)
        raise HTTPException(status_code=401, detail="账号或密码错误")
    token, expires_at = request.app.state.admin_tokens.issue()
    LOGGER.info("admin login succeeded")
    return {"token": token, "username": settings.admin_username, "expires_at": expires_at}


@router.post("/logout")
def logout(request: Request) -> dict:
    request.app.state.admin_tokens.revoke(bearer_token(request))
    return {"ok": True}


@router.get("/status")
def status(request: Request, runtime: Runtime) -> dict:
    admin = is_admin(request)
    return {"admin": admin, "username": runtime.settings.admin_username if admin else None}
