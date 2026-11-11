"""运行时：组装各组件，管理检视工作线程、延后任务调度线程和 CodeHub 回写线程。

- 检视队列由 MAX_REVIEW_CONCURRENCY 个工作线程消费，队列中的任务在控制台可见。
- 数据库是唯一的状态来源：启动时恢复中断的任务与未发送的评论 / 评价。
"""
from __future__ import annotations

import logging
import queue
import threading

from .codehub.client import CodeHubClient
from .codehub.publisher import Publisher
from .config import Settings
from .llm.codex import CodexClient
from .llm.registry import ModelRegistry
from .models import ReviewTask
from .review.codegraph import CodeGraphIndexer
from .review.git import RepositoryManager
from .review.scope import ReviewScopeRegistry
from .review.service import ReviewService
from .storage import Database

LOGGER = logging.getLogger(__name__)
SCHEDULER_INTERVAL_SECONDS = 30.0


class ReviewRuntime:
    def __init__(self, settings: Settings):
        settings.ensure_directories()
        self.settings = settings
        self.database = Database(settings.database_path)
        self.registry = ModelRegistry(self.database, settings)
        self.scope = ReviewScopeRegistry(self.database)
        self.codehub = CodeHubClient(settings)
        self.publisher = Publisher(self.database, self.codehub, settings)
        self.codex = CodexClient(settings, self.registry)
        self.codegraph = CodeGraphIndexer(settings)
        self.service = ReviewService(
            settings=settings,
            database=self.database,
            repositories=RepositoryManager(settings),
            codex=self.codex,
            registry=self.registry,
            codegraph=self.codegraph,
            publisher=self.publisher,
        )
        self._queue: queue.Queue[ReviewTask | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._active = 0
        self._active_lock = threading.Lock()

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        recovered = self.database.recover_pending_tasks()
        for task in recovered:
            self.enqueue(task)
        for finding_id in self.database.pending_comment_finding_ids():
            self.publisher.enqueue_comment(finding_id)
        for task_id in self.database.pending_evaluation_task_ids():
            self.publisher.enqueue_evaluation(task_id)
        self.publisher.start()
        self._threads = [
            threading.Thread(target=self._work, name=f"review-worker-{index}", daemon=True)
            for index in range(self.settings.max_review_concurrency)
        ]
        self._threads.append(threading.Thread(target=self._schedule, name="review-scheduler", daemon=True))
        for thread in self._threads:
            thread.start()
        LOGGER.info(
            "runtime started workers=%s recovered_reviews=%s pending_publish=%s",
            self.settings.max_review_concurrency, len(recovered), self.publisher.size(),
        )

    def stop(self, timeout: float = 15.0) -> None:
        if not self._threads:
            return
        self._stop.set()
        for _ in range(self.settings.max_review_concurrency):
            self._queue.put(None)
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads = []
        self.publisher.stop(timeout)
        self.codehub.close()
        LOGGER.info("runtime stopped queue_size=%s", self.queue_size())

    # ---------------------------------------------------------------- 队列

    def enqueue(self, task: ReviewTask) -> None:
        self._queue.put(task)

    def run_scheduled_now(self, task_id: str) -> bool:
        """管理员“立即执行”延后任务；任务不在等待状态时返回 False。"""
        if not self.database.claim_scheduled_now(task_id):
            return False
        self.enqueue(self.database.get_task(task_id))
        return True

    def queue_size(self) -> int:
        return self._queue.qsize()

    def active_reviews(self) -> int:
        with self._active_lock:
            return self._active

    # ---------------------------------------------------------------- 线程

    def _work(self) -> None:
        while (task := self._queue.get()) is not None and not self._stop.is_set():
            with self._active_lock:
                self._active += 1
            try:
                self.service.process(task)
            except Exception:
                # ReviewService 已处理业务异常；这里兜底防止工作线程退出
                LOGGER.exception("[task=%s] uncaught worker exception", task.id)
                self.database.mark_failed(task.id, "uncaught worker exception")
            finally:
                with self._active_lock:
                    self._active -= 1

    def _schedule(self) -> None:
        while not self._stop.wait(SCHEDULER_INTERVAL_SECONDS):
            try:
                for task in self.database.claim_due_scheduled_tasks():
                    LOGGER.info("[task=%s] scheduled review is due, queued", task.id)
                    self.enqueue(task)
            except Exception:
                LOGGER.exception("scheduler iteration failed")
