"""服务配置：只保留必须由部署方提供的参数，从环境变量 / .env 读取（大小写不敏感）。

调优类参数（超时、重试、并发、分片大小等）都是各模块内的常量，有合理默认值，无需配置；
模型路由与检视范围在 Web 控制台中维护。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    """把逗号分隔的配置拆成去空白、去空项的列表。"""
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # .env.local（可选，同样已 gitignore）覆盖 .env 中的同名项，适合放本机私有配置
        env_file=(".env", ".env.local"), env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---------- 大模型默认网关（OpenAI 兼容） ----------
    openai_api_base_url: str = ""
    openai_api_key: str = ""
    # 首次启动时生成“兜底”模型路由；之后以控制台保存的模型路由为准
    openai_model: str = ""

    # ---------- CodeHub ----------
    codehub_api_base_url: str = "https://repo-api-codeartsx-cn-southwest-2.sicarrier.com/api/v4"
    codehub_private_token: str = ""
    codehub_app_code: str = ""
    # 非空时校验 Webhook 请求头 X-CodeHub-Token
    codehub_webhook_secret: str = ""
    # 允许 clone 的仓库主机（逗号分隔）
    allowed_repository_hosts: str = "repo-git-codeartsx-cn-southwest-2.sicarrier.com"

    # ---------- 管理员（两项都配置才启用；未配置时控制台只读） ----------
    admin_username: str = ""
    admin_password: str = ""

    # ---------- 可选 ----------
    # 全局跳过的源分支（精确匹配，逗号分隔）
    skip_source_branches: str = "develop"
    # CodeGraph 符号 / 调用链索引（需本机安装 codegraph 并配置 Codex MCP；失败只降级）
    codegraph_enabled: bool = False
    # 运行时数据与日志目录
    data_dir: Path = Path("data")
    log_dir: Path = Path("log")

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
        for path in (self.repos_dir, self.worktrees_dir, self.codex_log_dir, self.prompt_log_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ---------- 解析后的配置 ----------
    def repository_hosts(self) -> frozenset[str]:
        return frozenset(host.lower() for host in _split_csv(self.allowed_repository_hosts))

    def skipped_source_branches(self) -> frozenset[str]:
        return frozenset(_split_csv(self.skip_source_branches))

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_username and self.admin_password)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
