"""单次 MR 检视主流程。

准备仓库 → 构建并过滤 Diff → 规划分片 →（大任务延后到晚间）→ 建 CodeGraph 索引
→ 分片并发调用 Codex → 定位校验 / 去重 → 落库 → 评论与 MR 评价入队 → 清理 clone。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..codehub.evaluation import build_content, rate_mr_quality
from ..codehub.publisher import Publisher
from ..config import Settings
from ..llm import chat
from ..llm.codex import DEFAULT_MODEL_LABEL, CodexClient, CodexError, CodexResult
from ..llm.output import Progress
from ..llm.registry import ModelRegistry, ModelRoute
from ..models import FindingCandidate, ReviewTask
from ..storage import Database
from .codegraph import CodeGraphIndexer
from .diff import ParsedDiff, ReviewChunk, filter_reviewable, plan_review_chunks
from .findings import deduplicate_findings, merge_verification, validate_findings
from .git import PreparedRepository, RepositoryManager
from .prompts import build_evaluation_prompt, build_large_file_prompt, build_review_prompt, build_verification_prompt

LOGGER = logging.getLogger(__name__)


@dataclass
class ChunkResult:
    """一个分片的检视结果（含多轮调用的累计统计）。"""

    findings: list[FindingCandidate] = field(default_factory=list)
    summary: str = ""
    raw_outputs: list[dict[str, str]] = field(default_factory=list)
    duration: float = 0.0
    passes: int = 0
    rejected: int = 0
    unverified: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    def add_run(self, label: str, result: CodexResult) -> None:
        self.passes += 1
        self.duration += result.duration_seconds
        self.input_tokens += result.input_tokens
        self.output_tokens += result.output_tokens
        self.cached_tokens += result.cached_input_tokens
        self.raw_outputs.append({"pass": label, "model": result.model, "output": result.raw_output})


class ReviewService:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        repositories: RepositoryManager,
        codex: CodexClient,
        registry: ModelRegistry,
        codegraph: CodeGraphIndexer,
        publisher: Publisher,
    ):
        self.settings = settings
        self.database = database
        self.repositories = repositories
        self.codex = codex
        self.registry = registry
        self.codegraph = codegraph
        self.publisher = publisher

    def _event(self, task_id: str, stage: str, status: str, message: str, detail: str | None = None) -> None:
        limit = self.settings.review_event_detail_max_chars
        self.database.append_event(
            task_id, stage=stage, status=status, message=message, detail=detail[:limit] if detail else None
        )

    # ================================================================ 主流程

    def process(self, task: ReviewTask) -> None:
        if not self.database.mark_running(task.id):
            LOGGER.info("[task=%s] skipped: task is no longer pending", task.id)
            return
        task = self.database.get_task(task.id) or task  # 以库中最新状态为准（如分片数、计划时间）
        self._event(task.id, "review", "started", "检视任务开始执行")
        prepared = None
        try:
            self._event(task.id, "repository", "started", "正在准备 Git 镜像与隔离 clone")
            prepared = self.repositories.prepare(task)
            self._event(task.id, "repository", "completed", "Git 仓库准备完成")
            self._review(task, prepared)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            LOGGER.exception("[task=%s][project=%s][mr=%s] review failed", task.id, task.project_id, task.mr_iid)
            self.database.mark_failed(task.id, error, exc.raw_output if isinstance(exc, CodexError) else None)
            self._event(task.id, "review", "error", "检视任务失败", error)
        finally:
            if prepared is not None:
                try:
                    self.repositories.cleanup(prepared)
                    self._event(task.id, "repository.cleanup", "completed", "隔离 clone 已清理")
                except Exception:
                    LOGGER.exception("[task=%s] clone cleanup failed", task.id)

    def _review(self, task: ReviewTask, prepared: PreparedRepository) -> None:
        worktree = prepared.worktree_path
        route = self.registry.route_for(task.repository_name)
        # ---------- 构建检视范围
        self._event(task.id, "diff", "started", "正在构建 MR 检视范围")
        built = self.repositories.build_diff(prepared, task)
        diff = filter_reviewable(built.parsed, self.settings.skip_patterns())
        files = diff.reviewable_files
        added_lines = sum(len(file.added_lines) for file in files)
        self._event(
            task.id, "diff", "completed", "检视范围构建完成",
            f"mode={built.mode}; files={len(files)}; added_lines={added_lines}; "
            f"source_unique_commits={built.source_unique_commits}; "
            f"target_unique_commits={built.target_unique_commits}; base={built.base_sha}",
        )
        scope_stats = {"review_mode": built.mode, "reviewed_file_count": len(files), "reviewed_added_line_count": added_lines}
        prompt_task = task.model_copy(update={"target_sha": built.target_sha, "review_mode": built.mode})

        if not files:
            self.database.complete_review(
                task.id, [], stats={**scope_stats, "review_summary": "没有可进行语义检视的代码变更。"}
            )
            self._event(task.id, "review", "completed", "任务完成：没有可检视的代码变更")
            self._queue_evaluation(task.id, route)
            return

        # ---------- 规划分片
        chunks = plan_review_chunks(
            diff, self.settings.max_prompt_diff_bytes, large_new_file_lines=self.settings.large_new_file_line_threshold
        )
        if len(chunks) > self.settings.max_review_chunks:
            reason = f"检视分片 {len(chunks)} 个超过上限 {self.settings.max_review_chunks} 个，已过滤代码检视"
            self.database.mark_ignored(task.id, reason)
            self._event(task.id, "queue", "warning", reason)
            return
        # 只在首次规划时延后；已延后过的任务（到点或管理员立即执行）不再重复延后
        if task.review_chunk_count == 0 and (start_at := self._deferred_start(len(chunks))):
            self.database.mark_scheduled(task.id, start_at=start_at, chunk_count=len(chunks))
            self._event(
                task.id, "queue", "pending",
                f"检视分片 {len(chunks)} 个，已安排至 {self.settings.large_review_start_hour:02d}:00 后执行",
                f"scheduled_start_at={start_at.isoformat()}",
            )
            return
        self.database.set_review_chunk_count(task.id, len(chunks))

        # ---------- 检视
        self._build_codegraph(task.id, worktree)
        self.database.record_model_usage(task.id, route.name if route else DEFAULT_MODEL_LABEL)
        direct = [chunk for chunk in chunks if chunk.direct_llm]
        workers = min(len(chunks), self.settings.max_chunk_review_concurrency)
        self._event(
            task.id, "quality", "progress",
            f"Diff 已规划为 {len(chunks)} 个检视单元（常规分片 {len(chunks) - len(direct)} 个，"
            f"超 {self.settings.large_new_file_line_threshold} 行新增文件直检 {len(direct)} 个），"
            f"最多 {workers} 个分片并发检视，不截断任何 hunk",
            "; ".join(chunk.diff.files[0].path for chunk in direct) or None,
        )
        with concurrent.futures.ThreadPoolExecutor(workers, thread_name_prefix=f"chunk-{task.id[:8]}") as pool:
            futures = [
                pool.submit(self._review_chunk, prompt_task, worktree, chunk, index, len(chunks), route)
                for index, chunk in enumerate(chunks, start=1)
            ]
            results = [future.result() for future in futures]

        # ---------- 汇总落库
        findings = deduplicate_findings([item for result in results for item in result.findings])
        rejected = sum(result.rejected for result in results)
        unverified = sum(result.unverified for result in results)
        self._event(
            task.id, "quality", "completed",
            f"质量门禁完成：{len(findings)} 条有效意见，其中 {unverified} 条待用户确认，拒绝 {rejected} 条非法定位或结构",
        )
        finding_ids = self.database.complete_review(
            task.id,
            findings,
            stats={
                **scope_stats,
                "review_summary": "\n".join(r.summary for r in results if r.summary)[:4000],
                "raw_codex_output": json.dumps([o for r in results for o in r.raw_outputs], ensure_ascii=False)[
                    :2_000_000
                ],
                "codex_duration": sum(r.duration for r in results),
                "rejected_finding_count": rejected,
                "review_pass_count": sum(r.passes for r in results),
                "token_input": sum(r.input_tokens for r in results),
                "token_output": sum(r.output_tokens for r in results),
                "token_cached_input": sum(r.cached_tokens for r in results),
            },
        )
        self._event(task.id, "review", "completed", f"检视完成：发现 {len(findings)} 条有效问题")
        for finding_id in finding_ids:
            self.publisher.enqueue_comment(finding_id)
        if finding_ids:
            self._event(task.id, "comment", "pending", f"{len(finding_ids)} 条意见已进入评论发送队列")
        self._queue_evaluation(task.id, route)
        LOGGER.info("[task=%s] review success findings=%s rejected=%s", task.id, len(findings), rejected)

    def _deferred_start(self, chunk_count: int) -> datetime | None:
        """大任务（分片数超过阈值）白天提交时延后到晚间开始执行。"""
        if chunk_count <= self.settings.large_review_chunk_threshold:
            return None
        now = datetime.now(ZoneInfo(self.settings.review_schedule_timezone))
        if now.hour >= self.settings.large_review_start_hour:
            return None
        start = now.replace(hour=self.settings.large_review_start_hour, minute=0, second=0, microsecond=0)
        return start.astimezone(UTC)

    def _build_codegraph(self, task_id: str, worktree: Path) -> None:
        if not self.codegraph.enabled:
            return
        if not self.codegraph.installed:
            self._event(task_id, "codegraph", "warning", "CodeGraph CLI 不可用，本次检视降级为普通检索与源码阅读")
            return
        self._event(task_id, "codegraph", "started", "正在为任务 clone 建立 CodeGraph 索引（所有分片共用）")
        result = self.codegraph.build_index(worktree)
        if result.success:
            self._event(
                task_id, "codegraph", "completed",
                f"CodeGraph 索引就绪，后续分片将优先使用符号与调用链检索（耗时 {result.duration_seconds:.1f}s）",
            )
        else:
            self._event(task_id, "codegraph", "warning", "CodeGraph 索引初始化失败，已降级为普通 Codex 检视", result.error)

    # ================================================================ 单个分片

    def _run_codex(
        self, task: ReviewTask, worktree: Path, prompt: str, stage: str, label: str, route: ModelRoute | None
    ) -> CodexResult:
        def on_progress(progress: Progress) -> None:
            self._event(task.id, f"{stage}.reasoning", "completed", progress.message, progress.detail)

        return self.codex.review(
            worktree=worktree,
            prompt=prompt,
            task_id=task.id,
            label=label,
            route=route,
            log_identity=(task.repository_name, task.source_branch),
            on_progress=on_progress,
        )

    def _review_chunk(
        self, task: ReviewTask, worktree: Path, chunk: ReviewChunk, index: int, total: int, route: ModelRoute | None
    ) -> ChunkResult:
        result = ChunkResult()
        kind = "direct" if chunk.direct_llm else "discovery"
        stage = f"codex.{kind}.{index}"
        self._event(
            task.id, stage, "started",
            f"开始第 {index}/{total} 个超大新增文件的单轮直接检视" if chunk.direct_llm
            else f"开始第 {index}/{total} 个分片的候选问题发现",
        )
        prompt = build_large_file_prompt(task, chunk.diff) if chunk.direct_llm else build_review_prompt(task, chunk.diff)
        discovery = self._run_codex(task, worktree, prompt, stage, f"{kind}-{index}", route)
        result.add_run(f"{kind}-{index}", discovery)
        result.summary = discovery.output.summary.strip()
        discovered = self._validate(result, discovery.output.findings, chunk.diff)
        self._event(
            task.id, stage, "completed",
            f"{'超大新增文件直检' if chunk.direct_llm else '候选发现'}完成：模型输出 "
            f"{len(discovery.output.findings)} 条，{len(discovered)} 条通过新增行定位校验",
        )
        result.findings = discovered
        if chunk.direct_llm or not self.settings.codex_verification_enabled:
            return result

        # ---------- 可选：证据复核轮
        stage = f"codex.verify.{index}"
        self._event(task.id, stage, "started", f"开始第 {index}/{total} 个分片的证据门禁与漏项复核")
        try:
            verified = self._run_codex(
                task, worktree, build_verification_prompt(task, chunk.diff, discovered), stage, f"verify-{index}", route
            )
        except CodexError as exc:
            # 复核失败时保留发现轮的合法意见，并标注需要作者确认
            result.findings, result.unverified = merge_verification(discovered, [])
            self._event(task.id, stage, "warning", f"证据复核执行失败，保留 {result.unverified} 条候选并请用户确认", str(exc))
            return result
        result.add_run(f"verify-{index}", verified)
        result.summary = verified.output.summary.strip() or result.summary
        confirmed = self._validate(result, verified.output.findings, chunk.diff)
        result.findings, result.unverified = merge_verification(discovered, confirmed)
        self._event(
            task.id, stage, "completed",
            f"证据复核完成：复核输出 {len(confirmed)} 条，待用户确认 {result.unverified} 条",
        )
        return result

    @staticmethod
    def _validate(result: ChunkResult, candidates: list[FindingCandidate], diff: ParsedDiff) -> list[FindingCandidate]:
        validation = validate_findings(candidates, diff)
        result.rejected += len(validation.rejected)
        return deduplicate_findings(validation.accepted)

    # ================================================================ MR 整体评价

    def _queue_evaluation(self, task_id: str, route: ModelRoute | None) -> None:
        """只在 MR 首次打开（action=open）时发表整体评价；后续 update 不重复评价。"""
        task = self.database.get_task(task_id)
        if task is None or task.action != "open":
            return
        findings = self.database.list_findings(task_id)
        content = build_content(task, findings, self._evaluation_summary(task, findings, route))
        self.database.queue_evaluation(task_id, level=rate_mr_quality(findings), content=content)
        self.publisher.enqueue_evaluation(task_id)
        self._event(task_id, "evaluation", "pending", f"MR 整体评价已进入发送队列（{len(findings)} 条意见）")

    def _evaluation_summary(self, task: ReviewTask, findings, route: ModelRoute | None) -> str | None:
        """调用大模型生成两点评价；网关未配置或调用失败时返回 None（使用模板评价）。"""
        gateway = self.registry.gateway_for(route)
        model = route.name if route else self.settings.openai_model
        if not gateway.configured or not model:
            return None
        try:
            result = chat.complete(
                gateway,
                model,
                build_evaluation_prompt(task, findings),
                timeout=self.settings.openai_timeout_seconds,
                max_attempts=self.settings.openai_max_attempts,
            )
        except chat.ChatError as exc:
            LOGGER.warning("[task=%s] evaluation summary failed, using template: %s", task.id, exc)
            return None
        self.database.add_token_usage(
            task.id, input_tokens=result.input_tokens, output_tokens=result.output_tokens
        )
        return result.text or None
