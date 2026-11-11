"""SQLite 持久化。

每次操作使用独立的短连接，API 线程、检视线程和发送线程之间不共享游标。
所有状态以数据库为准：进程重启后可从库中恢复待执行任务和待发送评论。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import FindingCandidate, FindingRecord, ReviewEvent, ReviewTask
from .review.findings import normalize_title

REPLY_CATEGORIES = ("resolved", "no_risk", "no_problem", "other")
# 任务来源：CodeHub MR（结果回写 CodeHub）/ 本地提交（结果只在平台展示）
ORIGIN_CODEHUB = "codehub"
ORIGIN_LOCAL = "local"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_tasks (
    id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL,
    mr_iid INTEGER NOT NULL,
    repository_url TEXT NOT NULL,
    repository_name TEXT NOT NULL,
    target_repository_url TEXT,
    source_branch TEXT NOT NULL,
    target_branch TEXT NOT NULL,
    source_sha TEXT NOT NULL,
    old_source_sha TEXT,
    target_sha TEXT,
    action TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'codehub',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    assignee_id INTEGER,
    mr_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    scheduled_start_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','running','success','failed','ignored')),
    error_message TEXT,
    ignored_reason TEXT,
    review_mode TEXT,
    review_summary TEXT NOT NULL DEFAULT '',
    review_chunk_count INTEGER NOT NULL DEFAULT 0,
    reviewed_file_count INTEGER NOT NULL DEFAULT 0,
    reviewed_added_line_count INTEGER NOT NULL DEFAULT 0,
    finding_count INTEGER NOT NULL DEFAULT 0,
    rejected_finding_count INTEGER NOT NULL DEFAULT 0,
    review_pass_count INTEGER NOT NULL DEFAULT 0,
    codex_duration REAL,
    raw_codex_output TEXT,
    models_used TEXT NOT NULL DEFAULT '[]',
    token_input INTEGER NOT NULL DEFAULT 0,
    token_output INTEGER NOT NULL DEFAULT 0,
    token_cached_input INTEGER NOT NULL DEFAULT 0,
    evaluation_status TEXT,
    evaluation_level INTEGER,
    evaluation_content TEXT NOT NULL DEFAULT '',
    evaluation_error TEXT,
    evaluation_retry_count INTEGER NOT NULL DEFAULT 0,
    evaluation_sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_tasks_created_at ON review_tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_review_tasks_repository ON review_tasks(repository_name);
CREATE INDEX IF NOT EXISTS idx_review_tasks_status ON review_tasks(status);
CREATE INDEX IF NOT EXISTS idx_review_tasks_key ON review_tasks(project_id, mr_iid, source_sha, status);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_task_id TEXT NOT NULL REFERENCES review_tasks(id) ON DELETE CASCADE,
    new_path TEXT NOT NULL,
    old_path TEXT NOT NULL,
    new_line INTEGER NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('suggestion','minor','major')),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    comment_sent_at TEXT,
    comment_error TEXT,
    comment_retry_count INTEGER NOT NULL DEFAULT 0,
    comment_http_status INTEGER,
    discussion_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_findings_task ON findings(review_task_id);
CREATE INDEX IF NOT EXISTS idx_findings_comment_status ON findings(status);

CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_task_id TEXT NOT NULL REFERENCES review_tasks(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','started','progress','completed','warning','error')),
    message TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_events_task_id ON review_events(review_task_id, id);

CREATE TABLE IF NOT EXISTS runtime_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_reply_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    mr_iid INTEGER NOT NULL,
    discussion_id TEXT NOT NULL,
    note_key TEXT NOT NULL,
    author_id INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL CHECK(category IN ('resolved','no_risk','no_problem','other')),
    finding_id INTEGER REFERENCES findings(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, mr_iid, discussion_id, note_key)
);
CREATE INDEX IF NOT EXISTS idx_review_reply_events_finding ON review_reply_events(finding_id);

-- 旧版本的回复统计重建表已不再使用
DROP TABLE IF EXISTS processed_note_events;
DROP TABLE IF EXISTS review_reply_rebuild_progress;
"""

# 旧库升级时需要补齐的列（只增不删）
_TASK_MIGRATIONS = {
    "assignee_id": "INTEGER",
    "mr_url": "TEXT NOT NULL DEFAULT ''",
    "target_repository_url": "TEXT",
    "scheduled_start_at": "TEXT",
    "review_summary": "TEXT NOT NULL DEFAULT ''",
    "review_chunk_count": "INTEGER NOT NULL DEFAULT 0",
    "reviewed_file_count": "INTEGER NOT NULL DEFAULT 0",
    "reviewed_added_line_count": "INTEGER NOT NULL DEFAULT 0",
    "rejected_finding_count": "INTEGER NOT NULL DEFAULT 0",
    "review_pass_count": "INTEGER NOT NULL DEFAULT 0",
    "models_used": "TEXT NOT NULL DEFAULT '[]'",
    "token_input": "INTEGER NOT NULL DEFAULT 0",
    "token_output": "INTEGER NOT NULL DEFAULT 0",
    "token_cached_input": "INTEGER NOT NULL DEFAULT 0",
    "evaluation_status": "TEXT",
    "evaluation_level": "INTEGER",
    "evaluation_content": "TEXT NOT NULL DEFAULT ''",
    "evaluation_error": "TEXT",
    "evaluation_retry_count": "INTEGER NOT NULL DEFAULT 0",
    "evaluation_sent_at": "TEXT",
    "origin": "TEXT NOT NULL DEFAULT 'codehub'",
}

# 创建任务时由调用方提供的字段
TASK_INPUT_FIELDS = (
    "project_id",
    "mr_iid",
    "repository_url",
    "repository_name",
    "target_repository_url",
    "source_branch",
    "target_branch",
    "source_sha",
    "old_source_sha",
    "target_sha",
    "action",
    "title",
    "description",
    "author",
    "assignee_id",
    "mr_url",
    "origin",
)

# 去重：同一 MR 的同一提交已有进行中或成功的任务时不再重复检视
_ACTIVE_DUPLICATE_SQL = """
SELECT * FROM review_tasks
WHERE project_id = ? AND mr_iid = ? AND source_sha = ? AND status IN ('pending','running','success')
ORDER BY created_at DESC LIMIT 1
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def _now() -> str:
    return utc_now().isoformat()


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def derive_mr_url(repository_url: str, source_sha: str) -> str:
    """由 SSH 仓库地址推导 CodeHub 提交页面链接（repo-git-* 主机对应 repo-* 页面）。"""
    match = re.match(r"^(?:ssh|https?)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)\.git$", repository_url or "")
    sha = (source_sha or "").strip()
    if not match or not sha:
        return ""
    web_host = match.group(1).replace("repo-git", "repo", 1)
    return f"https://{web_host}/{match.group(2)}/commit/{sha}"


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    # ------------------------------------------------------------------ 基础

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock, self._connect() as connection:
            return connection.execute(sql, tuple(params))

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock, self._connect() as connection:
            return connection.execute(sql, tuple(params)).fetchone()

    def _all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock, self._connect() as connection:
            return connection.execute(sql, tuple(params)).fetchall()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            existing = {row[1] for row in connection.execute("PRAGMA table_info(review_tasks)")}
            if existing:
                for column, declaration in _TASK_MIGRATIONS.items():
                    if column not in existing:
                        connection.execute(f"ALTER TABLE review_tasks ADD COLUMN {column} {declaration}")
            finding_columns = {row[1] for row in connection.execute("PRAGMA table_info(findings)")}
            if finding_columns and "discussion_id" not in finding_columns:
                connection.execute("ALTER TABLE findings ADD COLUMN discussion_id TEXT")
            connection.executescript(_SCHEMA)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_findings_discussion ON findings(discussion_id)"
            )

    def is_healthy(self) -> bool:
        row = self._one("SELECT 1")
        return bool(row and row[0] == 1)

    # ------------------------------------------------------------------ 运行时配置

    def get_setting(self, key: str) -> str | None:
        row = self._one("SELECT value FROM runtime_settings WHERE key = ?", (key,))
        return str(row[0]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        self._execute(
            """
            INSERT INTO runtime_settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, value, _now()),
        )

    # ------------------------------------------------------------------ 行映射

    @staticmethod
    def _task(row: sqlite3.Row) -> ReviewTask:
        data = dict(row)
        data.pop("raw_codex_output", None)
        try:
            models = json.loads(data.get("models_used") or "[]")
        except json.JSONDecodeError:
            models = []
        data["models_used"] = [item for item in models if isinstance(item, str)]
        return ReviewTask.model_validate(data)

    @staticmethod
    def _finding(row: sqlite3.Row) -> FindingRecord:
        data = dict(row)
        data.pop("comment_http_status", None)
        return FindingRecord.model_validate(data)

    @staticmethod
    def _event(row: sqlite3.Row) -> ReviewEvent:
        return ReviewEvent.model_validate(dict(row))

    def _update_task(self, task_id: str, where: str = "", **fields: Any) -> bool:
        """按字段更新任务；where 为额外条件。返回是否有行被更新。"""
        assignments = ", ".join(f"{name} = ?" for name in fields)
        cursor = self._execute(
            f"UPDATE review_tasks SET {assignments} WHERE id = ? {where}",
            (*fields.values(), task_id),
        )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------ 任务创建

    @staticmethod
    def _insert_task(
        connection: sqlite3.Connection, fields: dict[str, Any], status: str, ignored_reason: str | None
    ) -> sqlite3.Row:
        task_id = str(uuid.uuid4())
        values = {name: fields.get(name) for name in TASK_INPUT_FIELDS}
        for name in ("title", "description", "author", "mr_url"):
            values[name] = values[name] or ""
        values["origin"] = values["origin"] or ORIGIN_CODEHUB
        columns = ["id", *TASK_INPUT_FIELDS, "created_at", "status", "ignored_reason"]
        connection.execute(
            f"INSERT INTO review_tasks ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            (task_id, *values.values(), _now(), status, ignored_reason),
        )
        return connection.execute("SELECT * FROM review_tasks WHERE id = ?", (task_id,)).fetchone()

    def create_task(self, *, status: str = "pending", ignored_reason: str | None = None, **fields: Any) -> ReviewTask:
        with self._lock, self._connect() as connection:
            return self._task(self._insert_task(connection, fields, status, ignored_reason))

    def create_task_if_not_duplicate(self, **fields: Any) -> tuple[ReviewTask, bool]:
        """原子地创建待检视任务；同一 MR 同一提交已有活动任务时返回旧任务和 False。"""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    _ACTIVE_DUPLICATE_SQL,
                    (fields["project_id"], fields["mr_iid"], fields["source_sha"]),
                ).fetchone()
                created = row is None
                if created:
                    row = self._insert_task(connection, fields, "pending", None)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._task(row), created

    def clone_task(self, task_id: str) -> ReviewTask | None:
        """复制一个任务的检视范围，生成新的待检视任务（管理员重新检视）。"""
        source = self.get_task(task_id)
        if source is None:
            return None
        return self.create_task(**source.model_dump(include=set(TASK_INPUT_FIELDS)))

    def delete_task(self, task_id: str) -> bool:
        return self._execute("DELETE FROM review_tasks WHERE id = ?", (task_id,)).rowcount > 0

    # ------------------------------------------------------------------ 任务查询

    def get_task(self, task_id: str) -> ReviewTask | None:
        row = self._one("SELECT * FROM review_tasks WHERE id = ?", (task_id,))
        return self._task(row) if row else None

    @staticmethod
    def _filters(repository: str | None, author: str | None, statuses: list[str] | None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if repository:
            clauses.append("repository_name LIKE ? ESCAPE '\\' COLLATE NOCASE")
            params.append(_like(repository))
        if author:
            clauses.append("author LIKE ? ESCAPE '\\' COLLATE NOCASE")
            params.append(_like(author))
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params.extend(statuses)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def list_tasks(
        self,
        *,
        repository: str | None = None,
        author: str | None = None,
        statuses: list[str] | None = None,
        sort: str = "desc",
        limit: int = 120,
        offset: int = 0,
    ) -> list[ReviewTask]:
        where, params = self._filters(repository, author, statuses)
        order = "ASC" if sort == "asc" else "DESC"
        rows = self._all(
            f"SELECT * FROM review_tasks{where} ORDER BY created_at {order} LIMIT ? OFFSET ?",
            (*params, max(1, limit), max(0, offset)),
        )
        return [self._task(row) for row in rows]

    def task_summary(
        self, *, repository: str | None = None, author: str | None = None, statuses: list[str] | None = None
    ) -> dict[str, Any]:
        """筛选条件下的总量与按代码仓的意见分布（不分页）。"""
        where, params = self._filters(repository, author, statuses)
        total = self._one(
            f"SELECT COUNT(*) AS reviews, COALESCE(SUM(finding_count), 0) AS findings FROM review_tasks{where}",
            params,
        )
        repositories = self._all(
            f"""
            SELECT repository_name AS name, COUNT(*) AS count, COALESCE(SUM(finding_count), 0) AS findings
            FROM review_tasks{where}
            GROUP BY repository_name
            ORDER BY findings DESC, count DESC, name COLLATE NOCASE
            """,
            params,
        )
        return {
            "review_count": int(total["reviews"]),
            "finding_count": int(total["findings"]),
            "repository_count": len(repositories),
            "repository_stats": [dict(row) for row in repositories],
        }

    def repositories(self) -> list[dict[str, Any]]:
        rows = self._all(
            """
            SELECT repository_name AS name, COUNT(*) AS review_count, MAX(created_at) AS latest_at
            FROM review_tasks GROUP BY repository_name ORDER BY latest_at DESC
            """
        )
        return [dict(row) for row in rows]

    def authors(self) -> list[str]:
        rows = self._all(
            "SELECT DISTINCT author FROM review_tasks WHERE author <> '' ORDER BY author COLLATE NOCASE"
        )
        return [row[0] for row in rows]

    def token_usage_summary(self, *, limit: int | None = None) -> dict[str, Any]:
        """Token 用量汇总；只统计有用量记录的任务。"""
        has_usage = "WHERE token_input > 0 OR token_output > 0"
        total = self._one(
            f"""
            SELECT COUNT(*) AS review_count,
                   COUNT(DISTINCT project_id || ':' || mr_iid) AS mr_count,
                   COALESCE(SUM(token_input), 0) AS input_tokens,
                   COALESCE(SUM(token_output), 0) AS output_tokens,
                   COALESCE(SUM(token_cached_input), 0) AS cached_input_tokens
            FROM review_tasks {has_usage}
            """
        )
        rows = self._all(
            f"SELECT * FROM review_tasks {has_usage} ORDER BY created_at DESC LIMIT ?",
            (limit if limit else -1,),
        )
        summary = {key: int(total[key]) for key in total.keys()}
        summary["total_tokens"] = summary["input_tokens"] + summary["output_tokens"]
        count = summary["review_count"]
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            summary[f"avg_{key}"] = round(summary[key] / count, 2) if count else 0.0
        return {"summary": summary, "items": [self._task(row).model_dump(mode="json") for row in rows]}

    # ------------------------------------------------------------------ 任务状态流转

    def recover_pending_tasks(self) -> list[ReviewTask]:
        """进程重启：把中断的 running 任务恢复为 pending，并返回需要立即执行的任务。"""
        self._execute(
            "UPDATE review_tasks SET status = 'pending', started_at = NULL, error_message = NULL "
            "WHERE status = 'running'"
        )
        rows = self._all(
            "SELECT * FROM review_tasks WHERE status = 'pending' AND scheduled_start_at IS NULL "
            "ORDER BY created_at"
        )
        return [self._task(row) for row in rows]

    def claim_due_scheduled_tasks(self) -> list[ReviewTask]:
        """领取已到计划时间的延后任务（清空计划时间，保证只被领取一次）。"""
        now = _now()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM review_tasks WHERE status = 'pending' AND scheduled_start_at IS NOT NULL "
                "AND scheduled_start_at <= ? ORDER BY created_at",
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute("UPDATE review_tasks SET scheduled_start_at = NULL WHERE id = ?", (row["id"],))
        return [self._task(row) for row in rows]

    def claim_scheduled_now(self, task_id: str) -> bool:
        """管理员“立即执行”：仅当任务仍在等待计划时间时清空计划时间并返回 True。"""
        return self._update_task(
            task_id,
            "AND status = 'pending' AND scheduled_start_at IS NOT NULL",
            scheduled_start_at=None,
        )

    def mark_running(self, task_id: str) -> bool:
        """pending -> running；任务已被删除或已在执行时返回 False。"""
        return self._update_task(
            task_id, "AND status = 'pending'", status="running", started_at=_now(), error_message=None
        )

    def mark_scheduled(self, task_id: str, *, start_at: datetime, chunk_count: int) -> None:
        self._update_task(
            task_id,
            status="pending",
            started_at=None,
            scheduled_start_at=_iso(start_at),
            review_chunk_count=chunk_count,
        )

    def mark_ignored(self, task_id: str, reason: str) -> None:
        self._update_task(task_id, status="ignored", ignored_reason=reason, scheduled_start_at=None, finished_at=_now())

    def mark_failed(self, task_id: str, error: str, raw_output: str | None = None) -> None:
        self._update_task(
            task_id, status="failed", finished_at=_now(), error_message=error[:4000], raw_codex_output=raw_output
        )

    def set_review_chunk_count(self, task_id: str, count: int) -> None:
        self._update_task(task_id, review_chunk_count=count)

    def record_model_usage(self, task_id: str, model: str) -> None:
        """追加使用过的模型（并发分片共享同一任务，需去重）。"""
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT models_used FROM review_tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                return
            models = json.loads(row[0] or "[]")
            if model not in models:
                models.append(model)
                connection.execute(
                    "UPDATE review_tasks SET models_used = ? WHERE id = ?",
                    (json.dumps(models, ensure_ascii=False), task_id),
                )

    def add_token_usage(self, task_id: str, *, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> None:
        self._execute(
            "UPDATE review_tasks SET token_input = token_input + ?, token_output = token_output + ?, "
            "token_cached_input = token_cached_input + ? WHERE id = ?",
            (max(0, input_tokens), max(0, output_tokens), max(0, cached_tokens), task_id),
        )

    def complete_review(
        self,
        task_id: str,
        findings: list[FindingCandidate],
        *,
        stats: dict[str, Any],
        comment_status: str = "pending",
    ) -> list[int]:
        """原子地写入意见并把任务置为 success，返回新意见 ID。

        stats 中的键直接对应 review_tasks 的列（review_mode、review_summary、codex_duration 等）。
        """
        ids: list[int] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # 任务在本事务前不可能成功，残留的 pending 意见只能来自被中断的旧执行
                connection.execute(
                    "DELETE FROM findings WHERE review_task_id = ? AND status = 'pending'", (task_id,)
                )
                for finding in findings:
                    cursor = connection.execute(
                        "INSERT INTO findings (review_task_id, new_path, old_path, new_line, severity, title, body, "
                        "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            task_id,
                            finding.new_path,
                            finding.old_path or "",
                            finding.new_line,
                            finding.severity,
                            finding.title.strip(),
                            finding.body.strip(),
                            comment_status,
                            _now(),
                        ),
                    )
                    ids.append(int(cursor.lastrowid))
                fields = {
                    **stats,
                    "status": "success",
                    "finished_at": _now(),
                    "finding_count": len(findings),
                    "error_message": None,
                }
                assignments = ", ".join(f"{name} = ?" for name in fields)
                connection.execute(
                    f"UPDATE review_tasks SET {assignments} WHERE id = ?", (*fields.values(), task_id)
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return ids

    # ------------------------------------------------------------------ 检视意见

    def list_findings(self, task_id: str) -> list[FindingRecord]:
        rows = self._all(
            "SELECT * FROM findings WHERE review_task_id = ? ORDER BY new_path, new_line, id", (task_id,)
        )
        return [self._finding(row) for row in rows]

    def get_finding_with_task(self, finding_id: int) -> tuple[FindingRecord, ReviewTask] | None:
        row = self._one("SELECT * FROM findings WHERE id = ?", (finding_id,))
        if row is None:
            return None
        task = self.get_task(row["review_task_id"])
        return (self._finding(row), task) if task else None

    def pending_comment_finding_ids(self) -> list[int]:
        return [int(row[0]) for row in self._all("SELECT id FROM findings WHERE status = 'pending' ORDER BY id")]

    def find_sent_duplicate(self, finding: FindingRecord, task: ReviewTask) -> FindingRecord | None:
        """同一 MR 后续提交会重复发现同一问题：返回已发送过的相同位置、相同标题的意见。"""
        rows = self._all(
            """
            SELECT f.* FROM findings f JOIN review_tasks t ON f.review_task_id = t.id
            WHERE t.project_id = ? AND t.mr_iid = ? AND f.status = 'sent' AND f.id != ?
              AND f.new_path = ? AND f.new_line = ?
            """,
            (task.project_id, task.mr_iid, finding.id, finding.new_path, finding.new_line),
        )
        title = normalize_title(finding.title)
        return next((self._finding(row) for row in rows if normalize_title(row["title"]) == title), None)

    def mark_comment_sent(self, finding_id: int, discussion_id: str | None) -> None:
        self._execute(
            "UPDATE findings SET status = 'sent', comment_sent_at = ?, comment_error = NULL, "
            "discussion_id = COALESCE(?, discussion_id) WHERE id = ?",
            (_now(), discussion_id[:256] if discussion_id else None, finding_id),
        )

    def mark_comment_attempt(self, finding_id: int, attempt: int) -> None:
        self._execute("UPDATE findings SET comment_retry_count = ? WHERE id = ?", (attempt, finding_id))

    def mark_comment_failed(self, finding_id: int, error: str, http_status: int | None) -> None:
        self._execute(
            "UPDATE findings SET status = 'failed', comment_error = ?, comment_http_status = ? WHERE id = ?",
            (error[:4000], http_status, finding_id),
        )

    def mark_comment_skipped(self, finding_id: int) -> None:
        self._execute("UPDATE findings SET status = 'skipped' WHERE id = ?", (finding_id,))

    # ------------------------------------------------------------------ MR 整体评价

    def queue_evaluation(self, task_id: str, *, level: int, content: str, status: str = "pending") -> None:
        self._update_task(
            task_id,
            evaluation_status=status,
            evaluation_level=level,
            evaluation_content=content[:2000],
            evaluation_error=None,
        )

    def pending_evaluation_task_ids(self) -> list[str]:
        rows = self._all(
            "SELECT id FROM review_tasks WHERE status = 'success' AND evaluation_status = 'pending' "
            "ORDER BY created_at"
        )
        return [str(row[0]) for row in rows]

    def mark_evaluation_attempt(self, task_id: str, attempt: int) -> None:
        self._update_task(task_id, evaluation_retry_count=attempt)

    def mark_evaluation_sent(self, task_id: str) -> None:
        self._update_task(task_id, evaluation_status="sent", evaluation_sent_at=_now(), evaluation_error=None)

    def mark_evaluation_failed(self, task_id: str, error: str) -> None:
        self._update_task(task_id, evaluation_status="failed", evaluation_error=error[:4000])

    # ------------------------------------------------------------------ 检视过程事件

    def append_event(
        self, task_id: str, *, stage: str, status: str, message: str, detail: str | None = None
    ) -> None:
        self._execute(
            "INSERT INTO review_events (review_task_id, stage, status, message, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, stage[:100], status, message.strip()[:1000], detail.strip()[:20000] if detail else None, _now()),
        )

    def list_events(
        self,
        task_ids: list[str],
        *,
        after_id: int = 0,
        limit: int = 1000,
        reasoning_only: bool = False,
    ) -> list[ReviewEvent]:
        """按 ID 递增返回若干任务的事件，用于增量拉取和 SSE 推送。"""
        if not task_ids:
            return []
        where = f"review_task_id IN ({','.join('?' * len(task_ids))}) AND id > ?"
        if reasoning_only:
            where += " AND stage LIKE '%.reasoning'"
        rows = self._all(
            f"SELECT * FROM review_events WHERE {where} ORDER BY id LIMIT ?",
            (*task_ids, max(0, after_id), max(1, min(limit, 5000))),
        )
        return [self._event(row) for row in rows]

    def last_event_id(self, task_id: str) -> int:
        row = self._one("SELECT MAX(id) FROM review_events WHERE review_task_id = ?", (task_id,))
        return int(row[0] or 0)

    # ------------------------------------------------------------------ 用户回复统计

    @staticmethod
    def _note_key(note_id: int | None, note: str) -> str:
        return f"id:{note_id}" if note_id is not None else f"body:{hashlib.sha256(note.encode()).hexdigest()}"

    def record_review_reply(
        self,
        *,
        project_id: int,
        mr_iid: int,
        discussion_id: str,
        author_id: int | None,
        note_id: int | None,
        note: str,
        category: str,
    ) -> bool:
        """记录一条用户回复并关联到对应意见；同一条回复重复推送时返回 False。"""
        if category not in REPLY_CATEGORIES:
            raise ValueError(f"unknown reply category: {category}")
        finding = self._one(
            """
            SELECT f.id FROM findings f JOIN review_tasks t ON t.id = f.review_task_id
            WHERE t.project_id = ? AND t.mr_iid = ? AND f.discussion_id = ?
            ORDER BY f.id DESC LIMIT 1
            """,
            (project_id, mr_iid, discussion_id),
        )
        cursor = self._execute(
            """
            INSERT INTO review_reply_events (project_id, mr_iid, discussion_id, note_key, author_id, body,
                                             category, finding_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, mr_iid, discussion_id, note_key) DO NOTHING
            """,
            (
                project_id,
                mr_iid,
                discussion_id,
                self._note_key(note_id, note),
                author_id or 0,
                note,
                category,
                int(finding[0]) if finding else None,
                _now(),
            ),
        )
        return cursor.rowcount == 1

    def review_reply_stats(self) -> dict[str, int]:
        """意见回复分布。没有任何回复的意见默认计为“已解决”。"""
        finding_count = int(self._one("SELECT COUNT(*) FROM findings")[0])
        rows = self._all(
            "SELECT category, COUNT(*) FROM review_reply_events WHERE finding_id IS NOT NULL GROUP BY category"
        )
        counts = {category: 0 for category in REPLY_CATEGORIES} | {row[0]: int(row[1]) for row in rows}
        replied = int(
            self._one("SELECT COUNT(DISTINCT finding_id) FROM review_reply_events WHERE finding_id IS NOT NULL")[0]
        )
        counts["resolved"] += max(0, finding_count - replied)
        return {
            **counts,
            "finding_count": finding_count,
            "classification_total": sum(counts[category] for category in REPLY_CATEGORIES),
        }
