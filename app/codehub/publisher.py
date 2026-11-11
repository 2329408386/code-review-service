"""CodeHub 回写线程：串行发送行内评论与 MR 整体评价。

- 全进程只有一个发送线程，所有写请求共用一个限流器（两次请求间隔 ≥ COMMENT_RATE_INTERVAL_SECONDS）。
- 待发送状态保存在数据库中；进程重启后由 ReviewRuntime 重新入队。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from ..config import Settings
from ..storage import Database
from .client import CodeHubClient, CodeHubError

LOGGER = logging.getLogger(__name__)
COMMENT = "comment"
EVALUATION = "evaluation"


class Publisher:
    def __init__(
        self,
        database: Database,
        client: CodeHubClient,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.database = database
        self.client = client
        self.interval = settings.comment_rate_interval_seconds
        self.retry_interval = settings.comment_retry_interval_seconds
        self.max_attempts = settings.comment_max_attempts
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 生命周期

    def enqueue_comment(self, finding_id: int) -> None:
        self._queue.put((COMMENT, finding_id))

    def enqueue_evaluation(self, task_id: str) -> None:
        self._queue.put((EVALUATION, task_id))

    def size(self) -> int:
        return self._queue.qsize()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if not self.is_alive():
            self._thread = threading.Thread(target=self._run, name="codehub-publisher", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)

    def _run(self) -> None:
        while (job := self._queue.get()) is not None:
            try:
                self.process(*job)
            except Exception:
                # 任何异常都不能让全局发送线程退出
                LOGGER.exception("publisher job %s failed unexpectedly", job)

    def process(self, kind: str, target: Any) -> None:
        """同步处理一个任务（发送线程调用；测试中也可直接调用）。"""
        if kind == COMMENT:
            self._send_comment(int(target))
        elif kind == EVALUATION:
            self._send_evaluation(str(target))

    # ---------------------------------------------------------------- 发送

    def _throttle(self) -> None:
        """在发起请求前预留时间槽：即使请求失败，下一次请求也要满足最小间隔。"""
        now = self._clock()
        if self._last_request_at is not None:
            remaining = self.interval - (now - self._last_request_at)
            if remaining > 0:
                self._sleep(remaining)
                now = self._clock()
        self._last_request_at = now

    def _deliver(
        self, task_id: str, stage: str, label: str, send: Callable[[], Any], on_attempt: Callable[[int], None]
    ) -> tuple[Any, CodeHubError | None]:
        """带限流与重试地执行 send，返回 (结果, 最终错误)。"""
        for attempt in range(1, self.max_attempts + 1):
            on_attempt(attempt)
            self._throttle()
            try:
                return send(), None
            except CodeHubError as exc:
                error = exc
            except Exception as exc:  # 未预期的异常同样记录为失败，不重试
                LOGGER.exception("[task=%s] unexpected error while sending %s", task_id, label)
                return None, CodeHubError(str(exc), kind="unexpected", retryable=False)
            self._event(task_id, stage, "warning", f"{label}第 {attempt}/{self.max_attempts} 次发送失败", str(error))
            if not error.retryable or attempt == self.max_attempts:
                return None, error
            self._sleep(self.retry_interval)
        return None, CodeHubError("no attempt made", kind="unexpected", retryable=False)

    def _send_comment(self, finding_id: int) -> None:
        record = self.database.get_finding_with_task(finding_id)
        if record is None or record[0].status == "sent":
            return
        finding, task = record
        duplicate = self.database.find_sent_duplicate(finding, task)
        if duplicate is not None:
            self.database.mark_comment_skipped(finding.id)
            self._event(
                task.id, COMMENT, "warning", f"意见 #{finding.id} 与已发送意见重复，跳过",
                f"{finding.display_path}:{finding.new_line} 重复于意见 #{duplicate.id}",
            )
            return
        label = f"意见 #{finding.id} "
        self._event(
            task.id, COMMENT, "started", f"开始发送意见 #{finding.id}",
            f"{finding.display_path}:{finding.new_line} [{finding.severity}]",
        )
        discussion_id, error = self._deliver(
            task.id, COMMENT, label,
            lambda: self.client.post_discussion(task, finding),
            lambda attempt: self.database.mark_comment_attempt(finding.id, attempt),
        )
        if error is None:
            self.database.mark_comment_sent(finding.id, discussion_id)
            self._event(task.id, COMMENT, "completed", f"意见 #{finding.id} 已回写 CodeHub")
        else:
            self.database.mark_comment_failed(finding.id, str(error), error.status_code)
            self._event(task.id, COMMENT, "error", f"意见 #{finding.id} 回写失败", str(error))

    def _send_evaluation(self, task_id: str) -> None:
        task = self.database.get_task(task_id)
        if task is None or task.evaluation_status != "pending":
            return
        self._event(task.id, EVALUATION, "started", "开始发送 MR 整体评价")
        _, error = self._deliver(
            task.id, EVALUATION, "MR 评价",
            lambda: self.client.post_evaluation(task),
            lambda attempt: self.database.mark_evaluation_attempt(task.id, attempt),
        )
        if error is None:
            self.database.mark_evaluation_sent(task.id)
            self._event(task.id, EVALUATION, "completed", "MR 整体评价已回写 CodeHub", f"level={task.evaluation_level}")
        else:
            self.database.mark_evaluation_failed(task.id, str(error))
            self._event(task.id, EVALUATION, "error", "MR 整体评价回写失败", str(error))

    def _event(self, task_id: str, stage: str, status: str, message: str, detail: str | None = None) -> None:
        try:
            self.database.append_event(task_id, stage=stage, status=status, message=message, detail=detail)
        except Exception:
            LOGGER.exception("[task=%s] unable to persist %s event", task_id, stage)
