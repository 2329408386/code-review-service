"""配置与 SQLite 存储。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from helpers import candidate, create_task, task_fields

from app.config import Settings
from app.logging_config import configure_logging
from app.storage import Database, derive_mr_url


# ---------------------------------------------------------------- 配置


def test_settings_defaults_and_helpers(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, skip_source_branches="develop, release ", allowed_repository_hosts="A.com,b.com")
    assert not settings.admin_enabled  # 默认不启用管理员
    assert settings.skipped_source_branches() == {"develop", "release"}
    assert settings.repository_hosts() == {"a.com", "b.com"}
    # 调优参数已改为内部常量，环境变量中残留的旧配置会被忽略
    assert Settings(_env_file=None, max_review_concurrency=1, codex_command="x")


def test_configure_logging_writes_files(tmp_path):
    import logging

    configure_logging(tmp_path)
    logging.getLogger("app.test").info("hello")
    logging.getLogger("request").info("req")
    for handler in logging.getLogger("app").handlers + logging.getLogger("request").handlers:
        handler.flush()
    assert "hello" in (tmp_path / "ei.log").read_text()
    assert "req" in (tmp_path / "request.log").read_text()


# ---------------------------------------------------------------- 任务


def test_create_and_duplicate_detection(database: Database):
    task, created = database.create_task_if_not_duplicate(**task_fields())
    assert created and task.status == "pending"
    again, created = database.create_task_if_not_duplicate(**task_fields())
    assert not created and again.id == task.id


def test_task_lifecycle(database: Database):
    task = create_task(database)
    assert database.mark_running(task.id)
    assert not database.mark_running(task.id)  # 只能从 pending 进入 running
    database.record_model_usage(task.id, "m1")
    database.record_model_usage(task.id, "m1")
    database.add_token_usage(task.id, input_tokens=10, output_tokens=3, cached_tokens=2)
    ids = database.complete_review(
        task.id, [candidate(), candidate(line=3, title="另一个")], stats={"review_mode": "full", "token_input": 5}
    )
    stored = database.get_task(task.id)
    assert stored.status == "success" and stored.finding_count == 2 and len(ids) == 2
    assert stored.models_used == ["m1"]
    assert stored.review_mode == "full"
    assert stored.token_input == 5  # stats 直接覆盖列
    assert database.pending_comment_finding_ids() == ids


def test_recover_and_scheduled_claims(database: Database):
    running = create_task(database, source_sha="c" * 40)
    database.mark_running(running.id)
    scheduled = create_task(database, source_sha="d" * 40)
    database.mark_scheduled(scheduled.id, start_at=datetime.now(UTC) - timedelta(minutes=1), chunk_count=5)
    future = create_task(database, source_sha="e" * 40)
    database.mark_scheduled(future.id, start_at=datetime.now(UTC) + timedelta(hours=1), chunk_count=5)

    recovered = {task.id for task in database.recover_pending_tasks()}
    assert recovered == {running.id}  # 延后任务不在这里恢复
    assert [task.id for task in database.claim_due_scheduled_tasks()] == [scheduled.id]
    assert database.claim_due_scheduled_tasks() == []  # 只领取一次
    assert database.claim_scheduled_now(future.id)
    assert not database.claim_scheduled_now(future.id)
    assert database.get_task(scheduled.id).review_chunk_count == 5


def test_list_filters_summary_and_tokens(database: Database):
    a = create_task(database, repository_name="alpha", author="Alice", source_sha="1" * 40)
    create_task(database, repository_name="beta", author="Bob", source_sha="2" * 40, status="ignored")
    database.complete_review(a.id, [candidate()], stats={"token_input": 100, "token_output": 10})

    assert [t.repository_name for t in database.list_tasks(repository="ALP")] == ["alpha"]
    assert [t.author for t in database.list_tasks(statuses=["ignored"])] == ["Bob"]
    assert database.list_tasks(repository="%") == []  # LIKE 通配符被转义
    summary = database.task_summary()
    assert summary["review_count"] == 2 and summary["finding_count"] == 1
    assert summary["repository_stats"][0]["name"] == "alpha"
    assert database.authors() == ["Alice", "Bob"]
    # 隐藏的代码仓不出现在列表、汇总与代码仓下拉中
    assert [t.repository_name for t in database.list_tasks(exclude=["alpha"])] == ["beta"]
    assert database.task_summary(exclude=["alpha"])["review_count"] == 1
    assert [row["name"] for row in database.repositories(exclude=["beta"])] == ["alpha"]


def test_stats_overview_scopes_and_dimensions(database: Database):
    a = create_task(database, repository_name="alpha", source_sha="1" * 40)
    b = create_task(database, repository_name="beta", source_sha="2" * 40)
    create_task(database, repository_name="beta", source_sha="3" * 40, status="ignored")
    ids = database.complete_review(
        a.id, [candidate(), candidate(line=3, title="b", severity="minor")],
        stats={"token_input": 100, "token_output": 10, "token_cached_input": 40}, models=["m1", "m2"],
    )
    database.complete_review(b.id, [candidate()], stats={"token_input": 7, "token_output": 3}, models=["m1"])
    database.record_model_call(a.id, "m1", input_tokens=60, output_tokens=6, duration=4.0)
    database.record_model_call(a.id, "m2", input_tokens=40, output_tokens=4, duration=2.0)
    database.record_model_call(a.id, "m2", success=False)
    database.record_model_call(b.id, "m1", input_tokens=7, output_tokens=3, duration=1.0)
    database.record_model_call("missing", "m1")  # 任务不存在时忽略
    database.mark_comment_sent(ids[0], "disc-1")
    database.record_review_reply(
        project_id=7, mr_iid=3, discussion_id="disc-1", author_id=5, note_id=1, note="无风险", category="no_risk"
    )

    data = database.stats_overview()
    totals = data["totals"]
    assert totals["review_count"] == 3 and totals["success_count"] == 2 and totals["ignored_count"] == 1
    assert totals["finding_count"] == 3 and totals["repository_count"] == 2
    assert totals["total_tokens"] == 120 and totals["avg_tokens"] == 60  # 只按有用量的检视求平均
    assert data["severity"] == {"major": 2, "minor": 1, "suggestion": 0}
    assert data["replies"]["no_risk"] == 1 and data["replies"]["resolved"] == 2
    assert sum(day["reviews"] for day in data["trend"]) == 3
    repos = {row["name"]: row for row in data["repositories"]}
    assert repos["alpha"]["total_tokens"] == 110 and repos["alpha"]["severity"]["minor"] == 1
    assert repos["alpha"]["replies"]["no_risk"] == 1 and repos["beta"]["replies"]["resolved"] == 1
    models = {row["name"]: row for row in data["models"]}
    assert models["m1"]["call_count"] == 2 and models["m1"]["finding_count"] == 2 and models["m1"]["review_count"] == 2
    assert models["m2"]["call_count"] == 2 and models["m2"]["failed_count"] == 1 and models["m2"]["avg_duration"] == 2.0

    only_beta = database.stats_overview(include=["beta"])
    assert only_beta["totals"]["review_count"] == 2 and [m["name"] for m in only_beta["models"]] == ["m1"]
    assert database.stats_overview(exclude=["alpha"])["replies"]["no_risk"] == 0
    assert database.stats_overview(include=[])["totals"]["review_count"] == 0
    assert database.stats_overview(since="2999-01-01")["totals"]["review_count"] == 0
    assert database.review_reply_stats(exclude=["alpha"])["finding_count"] == 1
    assert [f.model for f in database.list_findings(a.id)] == ["m1", "m2"]


def test_model_calls_backfill(tmp_path):
    import json
    import sqlite3

    database = Database(tmp_path / "r.db")
    single = create_task(database, source_sha="1" * 40)
    multi = create_task(database, source_sha="2" * 40)
    database.complete_review(single.id, [candidate()], stats={"token_input": 9, "token_output": 3, "review_pass_count": 2})
    database.complete_review(multi.id, [], stats={"token_input": 9, "token_output": 3, "review_pass_count": 2})
    with sqlite3.connect(database.path) as connection:  # 模拟升级前的库：没有模型调用记录
        connection.execute("UPDATE review_tasks SET models_used = ? WHERE id = ?", (json.dumps(["a"]), single.id))
        connection.execute("UPDATE review_tasks SET models_used = ? WHERE id = ?", (json.dumps(["a", "b"]), multi.id))
        connection.execute("DELETE FROM runtime_settings")

    upgraded = Database(database.path)
    models = {row["name"]: row for row in upgraded.stats_overview()["models"]}
    assert models["a"]["input_tokens"] == 9 + 5 and models["b"]["input_tokens"] == 4  # 多模型任务均分
    assert models["a"]["call_count"] == 3 and models["a"]["finding_count"] == 1
    Database(database.path)  # 只回填一次
    assert upgraded.stats_overview()["models"][0]["call_count"] == 3


def test_clone_and_delete(database: Database):
    task = create_task(database)
    database.complete_review(task.id, [candidate()], stats={})
    clone = database.clone_task(task.id)
    assert clone.id != task.id and clone.status == "pending" and clone.source_sha == task.source_sha
    assert database.delete_task(task.id)
    assert database.get_task(task.id) is None
    assert database.list_findings(task.id) == []


# ---------------------------------------------------------------- 意见与评价


def test_comment_states_and_sent_duplicate(database: Database):
    first = create_task(database, source_sha="1" * 40)
    [first_id] = database.complete_review(first.id, [candidate(title="空指针")], stats={})
    database.mark_comment_sent(first_id, "disc-1")
    second = create_task(database, source_sha="2" * 40)
    [second_id] = database.complete_review(second.id, [candidate(title=" 空指针 ")], stats={})
    finding, task = database.get_finding_with_task(second_id)
    duplicate = database.find_sent_duplicate(finding, task)
    assert duplicate is not None and duplicate.id == first_id

    database.mark_comment_attempt(second_id, 2)
    database.mark_comment_failed(second_id, "err", 500)
    failed = database.list_findings(second.id)[0]
    assert failed.status == "failed" and failed.comment_retry_count == 2 and failed.comment_error == "err"


def test_evaluation_states(database: Database):
    task = create_task(database)
    database.complete_review(task.id, [], stats={})
    database.queue_evaluation(task.id, level=4, content="好")
    assert database.pending_evaluation_task_ids() == [task.id]
    database.mark_evaluation_attempt(task.id, 1)
    database.mark_evaluation_sent(task.id)
    stored = database.get_task(task.id)
    assert stored.evaluation_status == "sent" and stored.evaluation_level == 4
    assert database.pending_evaluation_task_ids() == []


# ---------------------------------------------------------------- 事件与回复


def test_events_incremental_and_reasoning_filter(database: Database):
    task = create_task(database)
    database.append_event(task.id, stage="review", status="started", message="开始")
    database.append_event(task.id, stage="codex.discovery.1.reasoning", status="completed", message="推理", detail="d")
    events = database.list_events([task.id])
    assert [event.stage for event in events] == ["review", "codex.discovery.1.reasoning"]
    assert database.list_events([task.id], after_id=events[0].id) == events[1:]
    assert database.list_events([task.id], reasoning_only=True) == events[1:]
    assert database.last_event_id(task.id) == events[1].id


def test_reply_stats_and_dedup(database: Database):
    task = create_task(database)
    ids = database.complete_review(task.id, [candidate(), candidate(line=3, title="b")], stats={})
    database.mark_comment_sent(ids[0], "disc-1")
    kwargs = dict(project_id=7, mr_iid=3, discussion_id="disc-1", author_id=5, note_id=1, note="没问题")
    assert database.record_review_reply(**kwargs, category="no_problem")
    assert not database.record_review_reply(**kwargs, category="no_problem")
    stats = database.review_reply_stats()
    # 一条回复“没问题”，另一条未回复的意见计为已解决
    assert stats["no_problem"] == 1 and stats["resolved"] == 1
    assert stats["finding_count"] == 2 and stats["classification_total"] == 2


def test_derive_mr_url():
    url = derive_mr_url("ssh://git@repo-git-x.com:22/group/demo.git", "abc")
    assert url == "https://repo-x.com/group/demo/commit/abc"
    assert derive_mr_url("/local/path", "abc") == ""
