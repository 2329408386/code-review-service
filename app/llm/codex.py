"""Codex CLI 调用：在任务 clone 中执行 `codex exec`，流式解析进度并提取结构化结论。

并发由两级信号量控制：全局 MAX_CODEX_CONCURRENCY + 每条模型路由各自的并发上限。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path

from ..config import Settings
from ..models import ReviewOutput
from ..review.prompts import REVIEW_RETRY_HINT
from .output import Progress, agent_message, parse_review_output, progress_from_event, token_usage
from .registry import ModelRegistry, ModelRoute

LOGGER = logging.getLogger(__name__)
DEFAULT_MODEL_LABEL = "Codex 本机默认模型"
# 注入给 Codex 子进程的网关密钥环境变量名
GATEWAY_KEY_ENV = "CODE_REVIEW_LLM_API_KEY"
MAX_OUTPUT_CHARS = 2_000_000
# 不传给 Codex 子进程的环境变量（避免服务自身的凭据泄露给模型可执行的命令）
_SECRET_MARKERS = (
    "TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "APP_CODE", "API_KEY", "ACCESS_KEY", "AUTH",
    "COOKIE", "CREDENTIAL", "SESSION_KEY",
)


class CodexError(RuntimeError):
    def __init__(self, message: str, *, raw_output: str = ""):
        super().__init__(message)
        self.raw_output = raw_output


@dataclass(frozen=True)
class CodexResult:
    output: ReviewOutput
    raw_output: str
    duration_seconds: float
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


def safe_environment(gateway_key: str = "") -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not any(marker in key.upper() for marker in _SECRET_MARKERS)
    }
    if gateway_key:
        environment[GATEWAY_KEY_ENV] = gateway_key
    return environment


def _safe_filename(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in value.strip()).strip("._-")
    return (safe or "unknown")[:100]


class CodexClient:
    def __init__(self, settings: Settings, registry: ModelRegistry):
        self.settings = settings
        self.registry = registry
        self.command = shutil.which(settings.codex_command) or settings.codex_command
        self._slots = threading.BoundedSemaphore(settings.max_codex_concurrency)
        self._active = 0
        self._active_lock = threading.Lock()
        self._prompt_log_lock = threading.Lock()

    # ---------------------------------------------------------------- 状态

    @property
    def available(self) -> bool:
        return bool(shutil.which(self.settings.codex_command)) or Path(self.command).exists()

    @cached_property
    def version(self) -> str | None:
        if not self.available:
            return None
        try:
            result = subprocess.run(
                [self.command, "--version"], text=True, capture_output=True, timeout=10,
                env=safe_environment(), check=False,
            )
            return (result.stdout or result.stderr).strip()[:200] or None
        except (OSError, subprocess.SubprocessError):
            LOGGER.exception("Codex version check failed")
            return None

    def active_runs(self) -> int:
        with self._active_lock:
            return self._active

    # ---------------------------------------------------------------- 对外入口

    def review(
        self,
        *,
        worktree: Path,
        prompt: str,
        task_id: str,
        label: str,
        route: ModelRoute | None,
        log_identity: tuple[str, str],
        on_progress: Callable[[Progress], None] | None = None,
    ) -> CodexResult:
        """在全局与模型路由两级并发槽位内执行一次检视（含格式错误重试）。"""
        route_slot = self.registry.slot(route)
        if route_slot:
            route_slot.acquire()
        try:
            with self._slots:
                with self._active_lock:
                    self._active += 1
                try:
                    return self._run_with_retries(worktree, prompt, task_id, label, route, log_identity, on_progress)
                finally:
                    with self._active_lock:
                        self._active -= 1
        finally:
            if route_slot:
                route_slot.release()

    # ---------------------------------------------------------------- 执行

    def _command(self, worktree: Path, route: ModelRoute | None, output_path: Path) -> tuple[list[str], str]:
        """组装 codex exec 命令。配置了网关时用命令行覆盖 provider，否则使用本机 ~/.codex 配置。"""
        gateway = self.registry.gateway_for(route)
        command = [self.command, "exec"]
        if gateway.configured:
            provider = "model_providers.code-review-gateway"
            command += [
                "-c", "model_provider=code-review-gateway",
                "-c", f"{provider}.name=code-review-gateway",
                "-c", f"{provider}.base_url={gateway.base_url}",
                "-c", f"{provider}.env_key={GATEWAY_KEY_ENV}",
                "-c", f"{provider}.wire_api=responses",
            ]
        if route:
            command += ["-c", f"model_reasoning_effort={route.reasoning_effort}", "--model", route.name]
        command += [
            "-c", "model_reasoning_summary=none",
            "--json", "--skip-git-repo-check",
            "--output-last-message", str(output_path),
            "-C", str(worktree),
            "-",
        ]
        return command, gateway.api_key if gateway.configured else ""

    def _run_with_retries(
        self,
        worktree: Path,
        prompt: str,
        task_id: str,
        label: str,
        route: ModelRoute | None,
        log_identity: tuple[str, str],
        on_progress: Callable[[Progress], None] | None,
    ) -> CodexResult:
        if not self.available:
            raise CodexError(f"Codex CLI not found: {self.settings.codex_command}")
        model = route.name if route else DEFAULT_MODEL_LABEL
        totals = [0.0, 0, 0, 0]  # 耗时、输入、输出、缓存输入（失败的尝试也计入）
        last_error = CodexError("Codex did not run")
        for attempt in range(1, self.settings.codex_max_attempts + 1):
            attempt_prompt = prompt if attempt == 1 else f"{prompt}\n\n{REVIEW_RETRY_HINT}"
            self._append_prompt_log(log_identity, task_id, label, attempt, attempt_prompt)
            log_prefix = self.settings.codex_log_dir / f"{task_id}.{_safe_filename(label)}.attempt-{attempt}"
            try:
                raw, messages, usage, duration = self._run_once(
                    worktree, attempt_prompt, route, log_prefix, task_id, on_progress
                )
            except CodexError as exc:
                last_error = exc
                LOGGER.warning("[task=%s] Codex %s attempt %s failed: %s", task_id, label, attempt, exc)
                continue
            totals = [totals[0] + duration, totals[1] + usage[0], totals[2] + usage[1], totals[3] + usage[2]]
            # 优先使用 --output-last-message 文件，其次倒序尝试事件流中的 agent_message
            sources = list(dict.fromkeys(text for text in (raw, *reversed(messages)) if text))
            errors: list[str] = []
            for source in sources:
                try:
                    output = parse_review_output(source)
                except ValueError as exc:
                    errors.append(f"{type(exc).__name__}: {exc}"[:500])
                    continue
                LOGGER.info(
                    "[task=%s] Codex %s parsed findings=%s tokens_in=%s tokens_out=%s",
                    task_id, label, len(output.findings), totals[1], totals[2],
                )
                return CodexResult(
                    output=output,
                    raw_output=source[:MAX_OUTPUT_CHARS],
                    duration_seconds=totals[0],
                    model=model,
                    input_tokens=totals[1],
                    output_tokens=totals[2],
                    cached_input_tokens=totals[3],
                )
            detail = " | ".join(errors) or "no final output"
            last_error = CodexError(f"Codex returned no valid structured result: {detail}"[:4000], raw_output=raw)
            LOGGER.warning("[task=%s] Codex %s attempt %s invalid output: %s", task_id, label, attempt, detail)
        raise last_error

    def _run_once(
        self,
        worktree: Path,
        prompt: str,
        route: ModelRoute | None,
        log_prefix: Path,
        task_id: str,
        on_progress: Callable[[Progress], None] | None,
    ) -> tuple[str, list[str], tuple[int, int, int], float]:
        """执行一次 codex exec，返回 (最终输出文件内容, agent 消息列表, token 用量, 耗时)。"""
        output_path = Path(f"{log_prefix}.json")
        output_path.unlink(missing_ok=True)  # 任务恢复后会复用 ID，不能读到旧文件
        command, gateway_key = self._command(worktree, route, output_path)
        events: deque[str] = deque(maxlen=2000)
        stderr: deque[str] = deque(maxlen=2000)
        messages: deque[str] = deque(maxlen=20)
        usage = [0, 0, 0]
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=worktree,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=safe_environment(gateway_key),
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise CodexError(f"unable to start Codex: {exc}") from exc

        def read_stdout() -> None:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for index, value in enumerate(token_usage(event)):
                    usage[index] += value
                if message := agent_message(event):
                    messages.append(message[:MAX_OUTPUT_CHARS])
                progress = progress_from_event(event)
                if progress:
                    events.append(json.dumps(progress.__dict__, ensure_ascii=False) + "\n")
                    if on_progress:
                        try:
                            on_progress(progress)
                        except Exception:
                            LOGGER.exception("[task=%s] Codex progress callback failed", task_id)

        readers = [
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=lambda: stderr.extend(line[-10000:] for line in process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()
        timed_out = False
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            return_code = process.wait(timeout=self.settings.codex_timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(process)
            return_code = -9
        except BrokenPipeError:
            return_code = process.wait()
        finally:
            for reader in readers:
                reader.join(timeout=2)
        duration = time.monotonic() - started
        stderr_text = "".join(stderr)
        self._write_diagnostics(log_prefix, events, stderr_text, messages)
        LOGGER.info(
            "[task=%s] Codex finished exit=%s duration=%.1fs log=%s", task_id, return_code, duration, log_prefix.name
        )
        raw = ""
        if output_path.exists():
            raw = output_path.read_text(encoding="utf-8", errors="replace")[:MAX_OUTPUT_CHARS]
        if timed_out:
            raise CodexError(f"Codex timed out after {self.settings.codex_timeout_seconds}s", raw_output=raw)
        if return_code != 0:
            detail = stderr_text.strip()[-2000:] or "no stderr output"
            raise CodexError(f"Codex exited with code {return_code}: {detail}", raw_output=raw)
        return raw, list(messages), (usage[0], usage[1], usage[2]), duration

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        """结束整个进程组（Codex 会派生 shell 子进程）。"""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
                process.wait(timeout=2)
                return
            except ProcessLookupError:
                return
            except subprocess.TimeoutExpired:
                continue

    @staticmethod
    def _write_diagnostics(log_prefix: Path, events: deque[str], stderr: str, messages: deque[str]) -> None:
        """保存进度事件、stderr 与 agent 消息，便于排查；写失败不影响检视。"""
        try:
            Path(f"{log_prefix}.events.jsonl").write_text("".join(events), encoding="utf-8")
            Path(f"{log_prefix}.stderr.log").write_text(stderr[-MAX_OUTPUT_CHARS:], encoding="utf-8")
            Path(f"{log_prefix}.agent-messages.log").write_text(
                "\n\n--- agent message ---\n\n".join(messages), encoding="utf-8"
            )
        except OSError:
            LOGGER.warning("unable to write Codex diagnostics for %s", log_prefix.name)

    def _append_prompt_log(
        self, identity: tuple[str, str], task_id: str, label: str, attempt: int, prompt: str
    ) -> None:
        """按 “仓库-源分支” 追加 Prompt 日志，便于事后回看模型实际收到的内容。"""
        repository, branch = identity
        header = json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "task_id": task_id,
                "run_label": label,
                "attempt": attempt,
                "repository": repository,
                "source_branch": branch,
            },
            ensure_ascii=False,
        )
        path = self.settings.prompt_log_dir / f"{_safe_filename(repository)}-{_safe_filename(branch)}.log"
        try:
            with self._prompt_log_lock, path.open("a", encoding="utf-8") as file:
                file.write(f"\n\n{'=' * 80}\n{header}\n{'-' * 80}\n{prompt.rstrip()}\n")
        except OSError:
            LOGGER.exception("[task=%s] unable to append prompt log", task_id)
