"""Git 操作：仓库镜像缓存、每个任务独立的 clone、构建 MR 检视 Diff。"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..config import Settings
from ..models import ReviewTask
from .diff import ParsedDiff, parse_unified_diff

LOGGER = logging.getLogger(__name__)
SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedRepository:
    mirror_path: Path
    worktree_path: Path


@dataclass(frozen=True)
class BuiltDiff:
    parsed: ParsedDiff
    mode: str  # full：merge-base..source；incremental：上次检视的 source..本次 source
    base_sha: str
    target_sha: str
    source_unique_commits: int = 0
    target_unique_commits: int = 0
    merge_base_candidates: int = 1


def repository_name_from_url(url: str) -> str:
    value = url.rstrip("/")
    parsed = urlparse(value)
    if parsed.path:
        value = parsed.path.rstrip("/")
    elif ":" in value and "@" in value:
        value = value.rsplit(":", 1)[-1]
    return (value.rsplit("/", 1)[-1] or "unknown-repository").removesuffix(".git")


def validate_repository_url(url: str, *, allowed_hosts: frozenset[str], allow_local_paths: bool) -> None:
    """Webhook 提供的 clone 地址必须位于配置的主机白名单内（防止被诱导访问任意地址）。"""
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme == "file":
        if allow_local_paths and Path(parsed.path).is_absolute():
            return
        raise GitError("local file repository URLs are disabled")
    if parsed.scheme:
        if parsed.scheme not in {"ssh", "https", "http"}:
            raise GitError(f"repository URL scheme is not allowed: {parsed.scheme}")
        host = parsed.hostname or ""
    elif match := re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", value):  # scp 风格 git@host:path
        host = match.group(1)
    elif allow_local_paths and Path(value).is_absolute():
        return
    else:
        raise GitError("local repository paths are disabled")
    if host.lower() not in allowed_hosts:
        raise GitError("repository host is not in ALLOWED_REPOSITORY_HOSTS")


class RepositoryManager:
    """镜像缓存 + 每个任务一份物理独立的 clone（Codex 可在其中随意读写，不影响缓存）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---------------------------------------------------------------- 基础

    def _git(self, *args: str, check: bool = True, env: dict | None = None, raw: bool = False) -> str:
        """执行 git 命令并返回 stdout（默认去除首尾空白）；失败且 check=False 时返回空串。

        日志与异常中隐藏仓库 URL。
        """
        safe = " ".join("<repository-url>" if "://" in arg else arg for arg in args)
        try:
            result = subprocess.run(
                ["git", *args],
                env={**os.environ, **env} if env else None,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.settings.git_command_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git command timed out: git {safe}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-2000:]
            raise GitError(f"git command failed ({result.returncode}): git {safe}; {detail}")
        if result.returncode != 0:
            return ""
        return result.stdout if raw else result.stdout.strip()

    def _succeeds(self, *args: str) -> bool:
        try:
            self._git(*args)
            return True
        except GitError:
            return False

    def _lock(self, repository_url: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(repository_url, threading.Lock())

    def mirror_path(self, repository_url: str) -> Path:
        key = hashlib.sha256(repository_url.encode()).hexdigest()[:16]
        return self.settings.repos_dir / f"{key}.git"

    def _verify_commit(self, mirror: Path, sha: str, label: str) -> str:
        if not SHA_RE.fullmatch(sha):
            raise GitError(f"invalid {label} commit id")
        resolved = self._git("--git-dir", str(mirror), "rev-parse", "--verify", f"{sha}^{{commit}}", check=False)
        if not resolved:
            raise GitError(f"{label} commit does not exist in repository cache: {sha}")
        return resolved

    # ---------------------------------------------------------------- 准备仓库

    def _ensure_mirror(self, repository_url: str) -> Path:
        mirror = self.mirror_path(repository_url)
        if not mirror.exists():
            LOGGER.info("[repo=%s] cloning mirror", repository_url)
            self._git("clone", "--mirror", repository_url, str(mirror))
            return mirror
        if not (mirror / "HEAD").exists():
            raise GitError(f"repository cache is not a valid bare mirror: {mirror}")
        if self._git("--git-dir", str(mirror), "remote", "get-url", "origin") != repository_url:
            raise GitError(f"project repository URL changed; refusing to reuse mirror for {repository_url}")
        self._git("--git-dir", str(mirror), "fetch", "--prune", "--tags", "origin")
        return mirror

    def prepare(self, task: ReviewTask) -> PreparedRepository:
        with self._lock(task.repository_url):
            mirror = self._ensure_mirror(task.repository_url)
            # Fork MR：目标分支的提交在官方仓库里，拉到私有命名空间，避免覆盖 fork 的引用
            target_url = (task.target_repository_url or "").strip()
            if target_url and target_url != task.repository_url:
                self._git(
                    "--git-dir", str(mirror), "fetch", "--force", target_url,
                    "+refs/heads/*:refs/review/target/heads/*",
                )
            self._verify_commit(mirror, task.source_sha, "source")
            if task.old_source_sha:
                self._verify_commit(mirror, task.old_source_sha, "old source")
            if task.target_sha:
                self._verify_commit(mirror, task.target_sha, "target")
            worktree = self.settings.worktrees_dir / task.id
            self._remove_clone(worktree)
            try:
                # --no-hardlinks：任务 clone 与镜像不共享任何对象文件
                self._git("clone", "--no-hardlinks", "--no-checkout", "--quiet", str(mirror), str(worktree))
                # 检视用 clone 不保留推送目标
                self._git("-C", str(worktree), "remote", "remove", "origin")
                # 检视不需要 LFS 大文件内容，跳过下载避免代理环境下 checkout 失败
                self._git(
                    "-C", str(worktree), "checkout", "--detach", "--quiet", task.source_sha,
                    env={"GIT_LFS_SKIP_SMUDGE": "1"},
                )
            except Exception:
                self._remove_clone(worktree)
                raise
        LOGGER.info("[task=%s] repository clone ready path=%s", task.id, worktree)
        return PreparedRepository(mirror_path=mirror, worktree_path=worktree)

    def _remove_clone(self, worktree: Path) -> None:
        # 只删除 worktrees 目录下一级的任务目录，绝不跟随其它路径
        try:
            relative = worktree.resolve().relative_to(self.settings.worktrees_dir.resolve())
        except ValueError as exc:
            raise GitError(f"refusing to remove clone outside worktrees dir: {worktree}") from exc
        if len(relative.parts) != 1:
            raise GitError(f"refusing to remove unexpected nested clone: {worktree}")
        if worktree.exists():
            shutil.rmtree(worktree)

    def cleanup(self, prepared: PreparedRepository) -> None:
        self._remove_clone(prepared.worktree_path)

    # ---------------------------------------------------------------- 构建 Diff

    def _resolve_target(self, mirror: Path, task: ReviewTask) -> str:
        if task.target_sha:
            return self._verify_commit(mirror, task.target_sha, "target")
        for ref in (
            f"refs/heads/{task.target_branch}",
            f"refs/review/target/heads/{task.target_branch}",
            f"refs/remotes/origin/{task.target_branch}",
            task.target_branch,
        ):
            sha = self._git("--git-dir", str(mirror), "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
            if sha:
                return sha
        raise GitError(f"target branch cannot be resolved: {task.target_branch}")

    def _select_merge_base(self, worktree: Path, target_sha: str, source_sha: str) -> tuple[str, int]:
        """选择离 MR 最近的 merge base。

        两个长期分支互相合并（criss-cross）时会有多个共同祖先，`git merge-base` 可能挑到很旧的那个，
        导致 Diff 混入大量无关历史。这里在所有候选中选“与 source 的差异文件最少”的一个。
        """
        candidates = self._git("-C", str(worktree), "merge-base", "--all", target_sha, source_sha).split()
        if len(candidates) <= 1:
            return (candidates[0] if candidates else ""), len(candidates)

        def changed_files(base: str) -> int:
            output = self._git("-C", str(worktree), "diff", "--name-only", base, source_sha, "--", check=False)
            return len(output.splitlines())

        best = min(candidates, key=changed_files)
        LOGGER.info("[repo=%s] criss-cross merge bases=%s selected=%s", worktree, len(candidates), best)
        return best, len(candidates)

    def build_diff(self, prepared: PreparedRepository, task: ReviewTask) -> BuiltDiff:
        worktree = str(prepared.worktree_path)
        source_sha = task.source_sha
        target_sha = self._resolve_target(prepared.mirror_path, task)
        mr_base, candidates = self._select_merge_base(prepared.worktree_path, target_sha, source_sha)
        # 增量检视：只有 MR 的 merge base 是上次检视提交的祖先时才可用；
        # 否则（force-push / rebase）上次提交已不在 MR Diff 上，CodeHub 会拒绝评论位置，回退为全量。
        mode, base_sha = "full", mr_base
        if task.old_source_sha:
            if self._succeeds("-C", worktree, "merge-base", "--is-ancestor", mr_base, task.old_source_sha):
                mode, base_sha = "incremental", task.old_source_sha
            else:
                LOGGER.warning("[task=%s] old source diverges from MR base; using full diff", task.id)
        source_commits = int(self._git("-C", worktree, "rev-list", "--count", f"{base_sha}..{source_sha}"))
        target_commits = 0
        if mode == "full":
            target_commits = int(self._git("-C", worktree, "rev-list", "--count", f"{base_sha}..{target_sha}"))
        diff_text = self._git(
            "-C", worktree, "diff", "--no-ext-diff", "--find-renames",
            f"--unified={self.settings.review_diff_context_lines}", base_sha, source_sha, "--",
            raw=True,
        )
        parsed = parse_unified_diff(diff_text)
        LOGGER.info(
            "[task=%s] %s diff built base=%s target=%s files=%s", task.id, mode, base_sha, target_sha, len(parsed.files)
        )
        return BuiltDiff(
            parsed=parsed,
            mode=mode,
            base_sha=base_sha,
            target_sha=target_sha,
            source_unique_commits=source_commits,
            target_unique_commits=target_commits,
            merge_base_candidates=candidates,
        )
