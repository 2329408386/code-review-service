"""数据模型：Webhook 载荷、API 入参、领域记录。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["suggestion", "minor", "major"]
ReviewStatus = Literal["pending", "running", "success", "failed", "ignored"]
EventStatus = Literal["pending", "started", "progress", "completed", "warning", "error"]
ReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]

SEVERITY_LABELS = {"major": "严重", "minor": "一般", "suggestion": "建议"}


# ============ CodeHub MR Webhook ============


class CommitPayload(BaseModel):
    id: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    url: str = Field(default="", max_length=2000)


class ProjectPayload(BaseModel):
    id: int = Field(ge=1)


class RepositoryPayload(BaseModel):
    git_ssh_url: str = Field(min_length=1, max_length=2000)


class AuthorPayload(BaseModel):
    name: str = Field(default="", max_length=500)
    id: int | None = Field(default=None, ge=1)


class AuthorIdChange(BaseModel):
    current: int | None = Field(default=None, ge=1)


class ChangesPayload(BaseModel):
    author_id: AuthorIdChange | None = None


class MergeRequestAttributes(BaseModel):
    iid: int = Field(ge=1)
    author_id: int | None = Field(default=None, ge=1)
    source_branch: str = Field(default="", max_length=1000)
    target_branch: str = Field(default="", max_length=1000)
    last_commit: CommitPayload
    target_branch_commit: CommitPayload | None = None
    source: RepositoryPayload
    target: RepositoryPayload | None = None
    state: str = Field(default="", max_length=100)
    action: str = Field(default="", max_length=100)
    oldrev: str | None = Field(default=None, max_length=64)
    title: str = Field(default="", max_length=2000)
    work_in_progress: bool = False
    description: str = Field(default="", max_length=100000)
    author: AuthorPayload = Field(default_factory=AuthorPayload)


class MergeRequestWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project: ProjectPayload
    object_attributes: MergeRequestAttributes
    changes: ChangesPayload = Field(default_factory=ChangesPayload)
    author: AuthorPayload | None = None

    def assignee_id(self) -> int | None:
        """CodeHub 评论 / 评价需要的处理人 ID，按可靠程度依次回退。"""
        attrs = self.object_attributes
        candidates = (
            self.changes.author_id.current if self.changes.author_id else None,
            attrs.author_id,
            attrs.author.id,
            self.author.id if self.author else None,
        )
        return next((value for value in candidates if value is not None), None)


# ============ CodeHub Note Webhook（用户回复行内评论） ============


class NoteAttributes(BaseModel):
    author_id: int | None = Field(default=None, ge=1)
    discussion_id: str = Field(default="", max_length=256)
    id: int | None = Field(default=None, ge=1)
    note: str = Field(default="", max_length=100000)
    system: bool = False


class MergeRequestReference(BaseModel):
    iid: int = Field(ge=1)


class NoteWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project: ProjectPayload | None = None
    project_id: int | None = Field(default=None, ge=1)
    merge_request: MergeRequestReference
    object_attributes: NoteAttributes


# ============ 控制台 API 入参 ============


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ModelRouteInput(BaseModel):
    id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    concurrency: int = Field(default=1, ge=1, le=100)
    repositories: list[str] = Field(default_factory=list, max_length=1000)
    base_url: str = Field(default="", max_length=2000)
    # 留空表示保留已保存的 Key
    api_key: str = Field(default="", max_length=2000)
    reasoning_effort: ReasoningEffort = "medium"


class ModelConfigurationInput(BaseModel):
    models: list[ModelRouteInput] = Field(min_length=1, max_length=20)
    default_model: str | None = Field(default=None, min_length=1, max_length=200)


class ModelDiscoveryRequest(BaseModel):
    base_url: str = Field(default="", max_length=2000)
    api_key: str = Field(default="", max_length=2000)
    route_id: str | None = Field(default=None, max_length=128)


class ReviewScopeRuleInput(BaseModel):
    repository: str = Field(min_length=1, max_length=200)
    target_branches: list[str] = Field(min_length=1, max_length=200)


class ReviewScopeInput(BaseModel):
    rules: list[ReviewScopeRuleInput] = Field(default_factory=list, max_length=200)


class RepositoryGroupInput(BaseModel):
    name: str = Field(max_length=200)
    repositories: list[str] = Field(default_factory=list, max_length=1000)
    # 重命名前的组名：用于保留已保存的 CodeHub 凭据
    previous_name: str | None = Field(default=None, max_length=200)
    # 分组独立的 CodeHub 凭据；留空保持不变，clear_codehub=True 改回 .env 默认凭据
    codehub_private_token: str | None = Field(default=None, max_length=1000)
    codehub_app_code: str | None = Field(default=None, max_length=1000)
    clear_codehub: bool = False


class RepositoryCatalogInput(BaseModel):
    """代码仓管理：隐藏的代码仓与分组。"""

    hidden: list[str] = Field(default_factory=list, max_length=1000)
    groups: list[RepositoryGroupInput] = Field(default_factory=list, max_length=200)


# ============ 模型输出 ============


class FindingCandidate(BaseModel):
    """模型给出的一条检视意见（尚未校验定位）。模型偶尔附带的多余字段直接忽略。"""

    model_config = ConfigDict(extra="ignore")

    new_path: str = Field(min_length=1, max_length=1000)
    old_path: str | None = Field(default=None, max_length=1000)
    new_line: int = Field(ge=1)
    severity: Severity
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=8000)


class ReviewOutput(BaseModel):
    # 顶层保持严格且 findings 必填：避免把正文中扫描到的单条意见对象误当成“空结论”
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=4000)
    findings: list[FindingCandidate]


# ============ 持久化记录 ============


class ReviewTask(BaseModel):
    id: str
    project_id: int
    mr_iid: int
    repository_url: str
    repository_name: str
    target_repository_url: str | None = None
    source_branch: str
    target_branch: str
    source_sha: str
    old_source_sha: str | None = None
    target_sha: str | None = None
    action: str
    # codehub：MR 检视，结果回写 CodeHub；local：本地提交检视，结果只在平台展示
    origin: str = "codehub"
    title: str = ""
    description: str = ""
    author: str = ""
    assignee_id: int | None = None
    mr_url: str = ""
    created_at: datetime
    status: ReviewStatus = "pending"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    scheduled_start_at: datetime | None = None
    error_message: str | None = None
    ignored_reason: str | None = None
    review_mode: str | None = None
    review_summary: str = ""
    review_chunk_count: int = 0
    reviewed_file_count: int = 0
    reviewed_added_line_count: int = 0
    finding_count: int = 0
    rejected_finding_count: int = 0
    review_pass_count: int = 0
    codex_duration: float | None = None
    models_used: list[str] = Field(default_factory=list)
    token_input: int = 0
    token_output: int = 0
    token_cached_input: int = 0
    evaluation_status: str | None = None
    evaluation_level: int | None = None
    evaluation_content: str = ""
    evaluation_error: str | None = None
    evaluation_retry_count: int = 0
    evaluation_sent_at: datetime | None = None


class FindingRecord(BaseModel):
    id: int
    review_task_id: str
    new_path: str
    old_path: str
    new_line: int
    severity: Severity
    title: str
    body: str
    status: str  # pending / sent / failed / skipped
    created_at: datetime
    comment_sent_at: datetime | None = None
    comment_error: str | None = None
    comment_retry_count: int = 0
    discussion_id: str | None = None
    model: str = ""  # 产出该意见的模型（历史多模型任务为空）

    @property
    def display_path(self) -> str:
        """删除行意见的 new_path 是 /dev/null，展示时使用真实的旧路径。"""
        return self.old_path if self.new_path == "/dev/null" else self.new_path

    def comment_body(self) -> str:
        return f"【{SEVERITY_LABELS[self.severity]}】{self.title.strip()}：{self.body.strip()}"


class ReviewEvent(BaseModel):
    id: int
    review_task_id: str
    stage: str
    status: EventStatus
    message: str
    detail: str | None = None
    created_at: datetime
