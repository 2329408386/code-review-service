"""服务配置：全部从环境变量 / .env 读取（大小写不敏感）。"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    """把逗号分隔的配置拆成去空白、去空项的列表。"""
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---------- 基础 ----------
    app_env: str = "development"
    data_dir: Path = Path("data")
    log_dir: Path = Path("log")
    host: str = "0.0.0.0"
    port: int = 8083

    # ---------- 管理员登录（两者都为空则关闭管理功能） ----------
    admin_username: str = "l00003834"
    admin_password: str = "123456"
    admin_token_ttl_seconds: int = Field(default=12 * 3600, ge=300)

    # ---------- CodeHub ----------
    codehub_api_base_url: str = "https://repo-api-codeartsx-cn-southwest-2.sicarrier.com/api/v4"
    codehub_private_token: str = ""
    codehub_app_code: str = ""
    # 非空时校验 Webhook 请求头 X-CodeHub-Token
    codehub_webhook_secret: str = ""
    # 允许 clone 的仓库主机（逗号分隔）；本地路径仅用于开发测试
    allowed_repository_hosts: str = "repo-git-codeartsx-cn-southwest-2.sicarrier.com"
    allow_local_repository_paths: bool = False
    comment_http_timeout_seconds: float = Field(default=20.0, gt=0)
    # CodeHub 要求两次写请求间隔不少于 5 秒
    comment_rate_interval_seconds: float = Field(default=5.1, ge=5.0)
    comment_retry_interval_seconds: float = Field(default=10.0, ge=0)
    comment_max_attempts: int = Field(default=3, ge=1, le=5)

    # ---------- 检视过滤 ----------
    # 全局跳过的源分支（精确匹配，逗号分隔）
    skip_source_branches: str = "develop"
    # 按仓库跳过的目标分支（JSON 列表）。若该仓库在“检视范围”里配置了白名单，以白名单为准。
    skip_review_rules: str = (
        '[{"repository": "ei-offline-microservice", '
        '"target_branches": ["feature/pa_agent", "feature/process_intelligent_pre_dev"]}]'
    )
    # 不参与检视的文件（fnmatch 模式，逗号分隔）
    review_skip_patterns: str = (
        "*.json,*.lock,package-lock.json,pnpm-lock.yaml,yarn.lock,*.min.js,*.min.css,*.map,"
        "*.html,*.htm,*.svg,*/node_modules/*,*/vendor/*,*/.git/*,*/generated/*,*/__generated__/*,"
        "*.generated.*,*.gen.*"
    )

    # ---------- 大模型（OpenAI 兼容网关） ----------
    # 作为模型路由未单独配置网关时的默认网关；Codex 与 MR 评价摘要都会使用它。
    openai_api_base_url: str = ""
    openai_api_key: str = ""
    # 首次启动时写入模型路由的默认模型；之后以 Web 控制台保存的路由为准。
    openai_model: str = ""
    openai_timeout_seconds: float = Field(default=120.0, gt=0)
    openai_max_attempts: int = Field(default=2, ge=1, le=5)

    # ---------- Codex CLI ----------
    codex_command: str = "codex"
    codex_timeout_seconds: float = Field(default=3600.0, gt=0)
    codex_max_attempts: int = Field(default=2, ge=1, le=3)
    # 可选的第二轮“证据复核”；位置校验始终由服务端确定性完成
    codex_verification_enabled: bool = False

    # ---------- 并发 ----------
    max_review_concurrency: int = Field(default=5, ge=1)
    max_codex_concurrency: int = Field(default=5, ge=1)
    max_chunk_review_concurrency: int = Field(default=5, ge=1)

    # ---------- Diff 切分 ----------
    git_command_timeout_seconds: float = Field(default=120.0, gt=0)
    review_diff_context_lines: int = Field(default=12, ge=3, le=100)
    # 单个分片的 Diff 字节上限（按完整文件 / hunk 切分，不截断）
    max_prompt_diff_bytes: int = Field(default=200_000, ge=10_000)
    # 分片数超过该值直接忽略，避免超大 MR 占满模型
    max_review_chunks: int = Field(default=100, ge=1)
    # 新增文件超过该行数时整文件单轮直检
    large_new_file_line_threshold: int = Field(default=2000, ge=1)

    # ---------- 大任务延后执行 ----------
    large_review_chunk_threshold: int = Field(default=3, ge=1)
    large_review_start_hour: int = Field(default=20, ge=0, le=23)
    review_schedule_timezone: str = "Asia/Shanghai"

    review_event_detail_max_chars: int = Field(default=4000, ge=500, le=20000)

    # ---------- CodeGraph（可选的符号 / 调用链索引，失败只降级） ----------
    codegraph_enabled: bool = False
    codegraph_command: str = "codegraph"
    codegraph_index_timeout_seconds: float = Field(default=1800.0, gt=0)

    # ---------- 派生路径 ----------
    @property
    def database_path(self) -> Path:
        return self.data_dir / "reviews.db"

    @property
    def repos_dir(self) -> Path:
        return self.data_dir / "repos"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def codex_log_dir(self) -> Path:
        return self.data_dir / "codex-logs"

    @property
    def prompt_log_dir(self) -> Path:
        return self.log_dir / "prompt"

    def ensure_directories(self) -> None:
        """创建运行目录。路径统一转为绝对路径，避免子进程以 worktree 为 cwd 时写错位置。"""
        self.data_dir = self.data_dir.expanduser().resolve()
        self.log_dir = self.log_dir.expanduser().resolve()
        for path in (
            self.repos_dir,
            self.worktrees_dir,
            self.codex_log_dir,
            self.prompt_log_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ---------- 解析后的配置 ----------
    def skip_patterns(self) -> tuple[str, ...]:
        return tuple(_split_csv(self.review_skip_patterns))

    def repository_hosts(self) -> frozenset[str]:
        return frozenset(host.lower() for host in _split_csv(self.allowed_repository_hosts))

    def skipped_source_branches(self) -> frozenset[str]:
        return frozenset(_split_csv(self.skip_source_branches))

    def skipped_target_branches(self, repository: str) -> frozenset[str]:
        """返回 SKIP_REVIEW_RULES 中该仓库需要跳过的目标分支；配置非法时视为空。"""
        try:
            rules = json.loads(self.skip_review_rules or "[]")
        except json.JSONDecodeError:
            return frozenset()
        branches: set[str] = set()
        for rule in rules if isinstance(rules, list) else []:
            if isinstance(rule, dict) and rule.get("repository") == repository:
                targets = rule.get("target_branches")
                if isinstance(targets, list):
                    branches.update(item for item in targets if isinstance(item, str))
        return frozenset(branches)

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_username and self.admin_password)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
