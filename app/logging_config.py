"""日志配置：业务日志写 ei.log（同时输出到控制台），HTTP 请求日志单独写 request.log。"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(filename)s:%(lineno)d %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
# 标记由本模块添加的 handler，重复调用时只替换自己的 handler（测试会多次创建应用）
_MANAGED = "_code_review_managed"


def _managed(handler: logging.Handler) -> logging.Handler:
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    setattr(handler, _MANAGED, True)
    return handler


def _reset(logger: logging.Logger, *handlers: logging.Handler) -> None:
    for handler in logger.handlers[:]:
        if getattr(handler, _MANAGED, False):
            logger.removeHandler(handler)
            handler.close()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in handlers:
        logger.addHandler(_managed(handler))


def _rotating(path: Path) -> RotatingFileHandler:
    return RotatingFileHandler(path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")


def configure_logging(log_dir: Path) -> None:
    directory = Path(log_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    _reset(logging.getLogger("app"), _rotating(directory / "ei.log"), logging.StreamHandler())
    _reset(logging.getLogger("request"), _rotating(directory / "request.log"))
    # 请求日志由中间件统一记录，关闭 uvicorn 自带的访问日志避免重复
    logging.getLogger("uvicorn.access").disabled = True
