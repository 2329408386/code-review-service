"""单次 MR 检视主流程：本地仓库 + 伪造 Codex CLI，评论回写用假 Publisher 记录。"""
from __future__ import annotations

from datetime import datetime

import pytest
from helpers import create_task, fake_codex, feature_repo

from app.llm import chat
from app.llm import codex as codex_module
from app.llm.codex import CodexClient
from app.llm.registry import ModelRegistry
from app.review import service as service_module
from app.review.codegraph import CodeGraphIndexer
from app.review.git import RepositoryManager
from app.review.service import ReviewService

FINDING = {"new_path": "app.py", "new_line": 3, "severity": "major", "title": "空指针访问", "body": "value 为 None"}
OUT_OF_DIFF = {**FINDING, "new_line": 1, "title": "越界"}


class RecordingPublisher:
    def __init__(self):
        self.comments: list[int] = []
        self.evaluations: list[str] = []

    def enqueue_comment(self, finding_id):
        self.comments.append(finding_id)

    def enqueue_evaluation(self, task_id):
        self.evaluations.append(task_id)


@pytest.fixture
def env(settings, database, tmp_path, monkeypatch):
    repo, target, source = feature_repo(tmp_path)

    def build(outputs, **codex_kwargs):
        fake_codex(tmp_path / "bin", outputs, monkeypatch=monkeypatch, **codex_kwargs)
        registry = ModelRegistry(database, settings)
        publisher = RecordingPublisher()
        service = ReviewService(
            settings=settings, database=database, repositories=RepositoryManager(settings),
            codex=CodexClient(settings, registry), registry=registry,
            codegraph=CodeGraphIndexer(settings), publisher=publisher,
        )
        task = create_task(
            database, repository_url=str(repo), source_sha=source, target_sha=target, **task_overrides
        )
        return service, task, publisher

    task_overrides: dict = {}
    build.task_overrides = task_overrides

    return build


def test_review_success_flow(env, database, settings):
    service, task, publisher = env([{"summary": "有问题", "findings": [FINDING, OUT_OF_DIFF]}])
    service.process(task)
    stored = database.get_task(task.id)
    assert stored.status == "success", stored.error_message
    assert stored.finding_count == 1 and stored.rejected_finding_count == 1
    assert stored.review_mode == "full" and stored.reviewed_added_line_count == 2
    assert stored.token_input == 100 and stored.review_summary == "有问题"
    [finding] = database.list_findings(task.id)
    assert (finding.new_path, finding.new_line) == ("app.py", 3)
    assert publisher.comments == [finding.id]
    # action=open 时生成 MR 评价（网关未配置，使用模板）
    assert publisher.evaluations == [task.id]
    assert stored.evaluation_status == "pending" and "总体评价" in stored.evaluation_content
    stages = {event.stage for event in database.list_events([task.id])}
    assert {"repository", "diff", "codex.discovery.1", "codex.discovery.1.reasoning", "repository.cleanup"} <= stages
    assert not list(settings.worktrees_dir.iterdir())  # clone 已清理


def test_update_action_skips_evaluation_and_rerun_guard(env, database):
    service, task, publisher = env([{"summary": "", "findings": []}])
    database._update_task(task.id, action="update")
    service.process(database.get_task(task.id))
    assert publisher.evaluations == []
    service.process(task)  # 已不是 pending，不会重复执行
    assert database.get_task(task.id).status == "success"


def test_codex_failure_marks_failed(env, database, settings, monkeypatch):
    monkeypatch.setattr(codex_module, "MAX_ATTEMPTS", 1)
    service, task, _ = env(["not json"])
    service.process(task)
    stored = database.get_task(task.id)
    assert stored.status == "failed" and "no valid structured result" in stored.error_message
    assert not list(settings.worktrees_dir.iterdir())


def test_local_commit_is_platform_only(env, database):
    env.task_overrides.update(origin="local", action="commit", project_id=0, mr_iid=0)
    service, task, publisher = env([{"summary": "", "findings": [FINDING]}])
    service.process(task)
    stored = database.get_task(task.id)
    assert stored.status == "success" and stored.origin == "local"
    # 意见与评价只保存在平台，不进入 CodeHub 发送队列，重启后也不会被恢复发送
    assert publisher.comments == [] and publisher.evaluations == []
    assert [f.status for f in database.list_findings(task.id)] == ["local"]
    assert database.pending_comment_finding_ids() == []
    assert stored.evaluation_status == "local" and "总体评价" in stored.evaluation_content
    assert database.pending_evaluation_task_ids() == []


def test_large_review_is_deferred_then_runs(env, database, monkeypatch):
    service, task, _ = env([{"summary": "", "findings": []}])
    monkeypatch.setattr(service_module, "DEFER_CHUNK_THRESHOLD", 0)  # 任何分片数都算大任务

    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 9, 0, tzinfo=tz)

    monkeypatch.setattr(service_module, "datetime", Morning)
    service.process(task)
    stored = database.get_task(task.id)
    assert stored.status == "pending" and stored.scheduled_start_at is not None
    assert stored.review_chunk_count == 1

    # 到点后再次执行：不再重复延后
    assert database.claim_scheduled_now(task.id)
    service.process(database.get_task(task.id))
    assert database.get_task(task.id).status == "success"


def test_too_many_chunks_ignored(env, database, monkeypatch):
    service, task, _ = env([{"summary": "", "findings": []}])
    monkeypatch.setattr(service_module, "MAX_CHUNKS", 1)
    monkeypatch.setattr(service_module, "MAX_CHUNK_BYTES", 10_000)
    service.repositories.build_diff = _fake_many_chunks(service.repositories.build_diff)
    service.process(task)
    stored = database.get_task(task.id)
    assert stored.status == "ignored" and "超过上限" in stored.ignored_reason


def _fake_many_chunks(original):
    """在真实 Diff 后追加多个大文件，使分片数超过上限。"""
    from app.review.diff import parse_unified_diff

    def build(prepared, task):
        built = original(prepared, task)
        for index in range(3):
            body = "".join(f"+{'x' * 100}\n" for _ in range(120))
            extra = f"diff --git a/f{index}.py b/f{index}.py\n--- a/f{index}.py\n+++ b/f{index}.py\n@@ -0,0 +1,120 @@\n{body}"
            built.parsed.files.extend(parse_unified_diff(extra).files)
        return built

    return build


def test_evaluation_uses_chat_summary(env, database, settings, monkeypatch):
    settings.openai_api_base_url = "http://gw/v1"
    settings.openai_api_key = "k"
    settings.openai_model = "gemini"
    service, task, _ = env([{"summary": "", "findings": []}])
    monkeypatch.setattr(
        service_module.chat, "complete",
        lambda *a, **k: chat.ChatResult("检视思路：逐行核对\n总体评价：良好", 7, 3),
    )
    service.process(task)
    stored = database.get_task(task.id)
    assert stored.evaluation_content == "检视思路：逐行核对\n总体评价：良好"
    assert stored.token_input >= 107  # Codex 100 + Chat 7
