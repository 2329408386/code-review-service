"""FastAPI 应用入口：组装运行时、请求日志中间件、API 路由与 Web 控制台静态资源。"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .api import auth, docs, local, reviews, settings as settings_api, stats, webhooks
from .api.deps import AdminTokens
from .config import Settings, get_settings
from .logging_config import configure_logging
from .runtime import ReviewRuntime

LOGGER = logging.getLogger(__name__)
REQUEST_LOGGER = logging.getLogger("request")
STATIC_DIR = Path(__file__).with_name("static")
WEBHOOK_PATHS = {"/api/webhooks/codehub", "/api/webhooks/codehub-note"}


def create_app(
    settings: Settings | None = None, *, runtime: ReviewRuntime | None = None, start_workers: bool = True
) -> FastAPI:
    """创建应用。测试时可注入 settings / runtime，并通过 start_workers=False 关闭后台线程。"""
    settings = settings or get_settings()
    if bool(settings.admin_username) != bool(settings.admin_password):
        raise ValueError("ADMIN_USERNAME and ADMIN_PASSWORD must be configured together")
    settings.ensure_directories()
    configure_logging(settings.log_dir)
    runtime = runtime or ReviewRuntime(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        LOGGER.info("application startup data_dir=%s", settings.data_dir)
        if not settings.admin_enabled:
            LOGGER.warning("ADMIN_USERNAME / ADMIN_PASSWORD 未配置：控制台为只读模式")
        if start_workers:
            runtime.start()
        try:
            yield
        finally:
            if start_workers:
                runtime.stop()
            LOGGER.info("application shutdown")

    app = FastAPI(title="XKL 代码检视平台", version="1.0.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.admin_tokens = AdminTokens()

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        """记录每个请求的耗时；Webhook 请求额外记录原始载荷便于排查。"""
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        client = request.client.host if request.client else "unknown"
        if request.url.path in WEBHOOK_PATHS:
            body = (await request.body()).decode("utf-8", errors="replace")
            REQUEST_LOGGER.info("[request=%s][client=%s] %s body=%s", request_id, client, request.url.path, body)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            REQUEST_LOGGER.exception("[request=%s] %s %s -> exception", request_id, request.method, request.url.path)
            raise
        response.headers["X-Request-ID"] = request_id
        REQUEST_LOGGER.info(
            "[request=%s][client=%s] %s %s -> %s %.1fms",
            request_id, client, request.method, request.url.path, response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return response

    for module in (auth, webhooks, local, reviews, settings_api, stats, docs):
        app.include_router(module.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    index_html = _versioned_index()

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        # 首页不缓存；脚本与样式地址带内容哈希，升级后浏览器不会继续使用旧文件
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    return app


def _versioned_index() -> str:
    """给 index.html 中的 app.js / style.css 引用加上内容哈希版本号。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "style.css"):
        digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f"/static/{name}", f"/static/{name}?v={digest}")
    return html


app = create_app()
