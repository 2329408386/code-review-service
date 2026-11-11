"""可选的 CodeGraph 索引：在任务 clone 中建一次索引，供该任务所有 Codex 分片共用。

它只是检索加速：命令缺失、超时或失败都只返回失败结果，绝不让检视任务失败。
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from ..config import Settings

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexResult:
    success: bool
    duration_seconds: float
    error: str | None = None


class CodeGraphIndexer:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.command = settings.codegraph_command

    @property
    def enabled(self) -> bool:
        return self.settings.codegraph_enabled

    @cached_property
    def installed(self) -> bool:
        return bool(shutil.which(self.command)) or Path(self.command).exists()

    @cached_property
    def version(self) -> str | None:
        if not self.installed:
            return None
        try:
            result = subprocess.run(
                [self.command, "--version"], text=True, capture_output=True, timeout=10, check=False
            )
            return (result.stdout or result.stderr).strip()[:200] or None
        except (OSError, subprocess.SubprocessError):
            LOGGER.exception("CodeGraph version check failed")
            return None

    def build_index(self, worktree: Path) -> IndexResult:
        """在 worktree 中执行 `codegraph init`，生成 .codegraph/ 索引。"""
        if not self.installed:
            return IndexResult(False, 0.0, "codegraph CLI not found")
        started = time.perf_counter()
        timeout = self.settings.codegraph_index_timeout_seconds
        try:
            result = subprocess.run(
                [self.command, "init"], cwd=worktree, text=True, capture_output=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired:
            error = f"codegraph init timed out after {timeout}s"
        except (OSError, subprocess.SubprocessError) as exc:
            error = f"codegraph init failed to start: {exc}"
        else:
            if result.returncode == 0:
                return IndexResult(True, time.perf_counter() - started)
            detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")[:300]
            error = f"codegraph init exit {result.returncode}: {detail or 'unknown error'}"
        LOGGER.warning("CodeGraph index failed in %s: %s", worktree, error)
        return IndexResult(False, time.perf_counter() - started, error)
