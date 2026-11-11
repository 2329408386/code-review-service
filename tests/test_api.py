"""HTTP 接口：Webhook、鉴权、检视记录、管理员操作、运行时配置、统计；以及运行时线程。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from helpers import candidate, codex_calls, create_task, fake_codex, feature_repo

from app.codehub.client import CodeHubError
from app.main import create_app
from app.runtime import ReviewRuntime


@pytest.fixture
def client(settings):
    settings.allowed_repository_hosts = "codehub.example.com"
    app = create_app(settings, start_workers=False)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def runtime(client) -> ReviewRuntime:
    return client.app.state.runtime


def login(client) -> dict:
    token = client.post("/api/auth/login", json={"username": "admin", "password": "secret"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def mr_payload(**attrs) -> dict:
    data = {
        "iid": 3, "state": "opened", "action": "open", "title": "新增功能",
        "source_branch": "feature/x", "target_branch": "main",
        "last_commit": {"id": "a" * 40}, "target_branch_commit": {"id": "b" * 40},
        "source": {"git_ssh_url": "git@codehub.example.com:group/demo.git"},
        "author": {"name": "alice", "id": 11},
    }
    data.update(attrs)
    return {"project": {"id": 7}, "object_attributes": data}


# ---------------------------------------------------------------- Webhook


def test_merge_request_webhook(client, runtime):
    response = client.post("/api/webhooks/codehub", json=mr_payload())
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    task = runtime.database.get_task(task_id)
    assert task.repository_name == "demo" and task.assignee_id == 11 and task.status == "pending"
    assert runtime.queue_size() == 1

    duplicate = client.post("/api/webhooks/codehub", json=mr_payload())
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "duplicate"

    ignored = client.post("/api/webhooks/codehub", json=mr_payload(state="merged"))
    assert ignored.json()["status"] == "ignored"
    assert runtime.database.get_task(ignored.json()["task_id"]).status == "ignored"

    rejected = client.post(
        "/api/webhooks/codehub", json=mr_payload(source={"git_ssh_url": "git@evil.com:g/r.git"}, last_commit={"id": "c" * 40})
    )
    assert rejected.status_code == 400
    assert client.post("/api/webhooks/codehub", json={"bad": 1}).status_code == 422


def test_webhook_secret(client, settings):
    settings.codehub_webhook_secret = "s3cret"
    assert client.post("/api/webhooks/codehub", json=mr_payload()).status_code == 401
    ok = client.post("/api/webhooks/codehub", json=mr_payload(), headers={"X-CodeHub-Token": "s3cret"})
    assert ok.status_code == 202


def test_note_webhook(client, runtime, monkeypatch):
    task = create_task(runtime.database)
    [finding_id] = runtime.database.complete_review(task.id, [candidate()], stats={})
    runtime.database.mark_comment_sent(finding_id, "disc-1")
    resolved: list[tuple] = []
    monkeypatch.setattr(runtime.codehub, "current_user_id", lambda: 99)
    monkeypatch.setattr(runtime.codehub, "resolve_discussion", lambda *args: resolved.append(args))

    def note(text, note_id=1, author_id=5, system=False):
        body = {
            "project": {"id": 7}, "merge_request": {"iid": 3},
            "object_attributes": {"discussion_id": "disc-1", "id": note_id, "note": text, "author_id": author_id, "system": system},
        }
        return client.post("/api/webhooks/codehub-note", json=body)

    assert note("x", system=True).json()["reason"] == "system note"
    assert note("确认", author_id=99).json()["reason"] == "service note"
    assert note("稍后处理", note_id=2).json()["reason"] == "no auto-resolve keyword"
    assert note("已确认").json()["action"] == "resolved"
    assert note("已确认").json()["reason"] == "duplicate note"
    assert resolved == [(7, 3, "disc-1")]

    def fail(*_):
        raise CodeHubError("down", kind="server_error", retryable=True)

    monkeypatch.setattr(runtime.codehub, "resolve_discussion", fail)
    assert note("无风险", note_id=3).status_code == 502
    stats = client.get("/api/stats/review-replies").json()
    assert stats["resolved"] == 1 and stats["other"] == 1 and stats["no_risk"] == 1


# ---------------------------------------------------------------- 鉴权


def test_auth_flow(client):
    assert client.get("/api/auth/status").json() == {"admin": False, "username": None}
    assert client.post("/api/auth/login", json={"username": "admin", "password": "bad"}).status_code == 401
    headers = login(client)
    assert client.get("/api/auth/status", headers=headers).json()["admin"]
    client.post("/api/auth/logout", headers=headers)
    assert not client.get("/api/auth/status", headers=headers).json()["admin"]


def test_admin_required(client, runtime):
    task = create_task(runtime.database, status="failed")
    assert client.post(f"/api/tasks/{task.id}/rerun").status_code == 401
    assert client.delete(f"/api/tasks/{task.id}").status_code == 401
    assert client.put("/api/settings/review-scope", json={"rules": []}).status_code == 401
    assert client.put("/api/settings/models", json={"models": [{"name": "m"}]}).status_code == 401


# ---------------------------------------------------------------- 检视记录


def test_review_list_detail_events_export(client, runtime):
    database = runtime.database
    task = create_task(database, title="=SUM(1)")
    database.complete_review(task.id, [candidate(), candidate(line=3, title="b")], stats={})
    database.append_event(task.id, stage="review", status="started", message="开始")
    database.append_event(task.id, stage="codex.discovery.1.reasoning", status="completed", message="推理")
    create_task(database, source_sha="c" * 40, status="ignored", repository_name="other")

    listing = client.get("/api/reviews", params={"status": ["success"], "page_size": 30}).json()
    assert listing["total_count"] == 1 and listing["items"][0]["id"] == task.id
    assert listing["summary"]["finding_count"] == 2
    assert client.get("/api/reviews", params={"page_size": 7}).json()["page_size"] == 120

    detail = client.get(f"/api/reviews/{task.id}").json()
    assert len(detail["findings"]) == 2 and detail["last_event_id"] > 0
    assert client.get("/api/reviews/missing").status_code == 404

    events = client.get(f"/api/reviews/{task.id}/events", params={"limit": 1}).json()
    assert len(events["items"]) == 1 and events["has_more"]
    reasoning = client.get(f"/api/reviews/{task.id}/events", params={"reasoning_only": True}).json()
    assert [item["stage"] for item in reasoning["items"]] == ["codex.discovery.1.reasoning"]

    export = client.get(f"/api/reviews/{task.id}/findings/export")
    text = export.content.decode("utf-8")
    assert text.startswith("﻿任务ID") and "'=SUM(1)" in text  # 防公式注入
    assert "attachment" in export.headers["content-disposition"]

    assert {repo["name"] for repo in client.get("/api/repositories").json()} == {"demo", "other"}
    assert client.get("/api/authors").json() == ["alice"]


def test_event_stream_validation(client):
    assert client.get("/api/review-events/stream").status_code == 422


def test_admin_task_operations(client, runtime):
    headers = login(client)
    database = runtime.database
    done = create_task(database, status="success")
    running = create_task(database, source_sha="c" * 40)
    database.mark_running(running.id)

    rerun = client.post(f"/api/tasks/{done.id}/rerun", headers=headers)
    assert rerun.status_code == 202 and rerun.json()["task_id"] != done.id
    assert client.post(f"/api/tasks/{rerun.json()['task_id']}/rerun", headers=headers).status_code == 409
    assert client.delete(f"/api/tasks/{running.id}", headers=headers).status_code == 409
    assert client.post(f"/api/tasks/{done.id}/run-now", headers=headers).status_code == 409
    assert client.delete(f"/api/tasks/{done.id}", headers=headers).json()["deleted"]
    assert database.get_task(done.id) is None


def test_run_now_for_scheduled_task(client, runtime):
    from datetime import UTC, datetime, timedelta

    headers = login(client)
    task = create_task(runtime.database)
    runtime.database.mark_scheduled(task.id, start_at=datetime.now(UTC) + timedelta(hours=3), chunk_count=9)
    before = runtime.queue_size()
    assert client.post(f"/api/tasks/{task.id}/run-now", headers=headers).status_code == 202
    assert runtime.queue_size() == before + 1
    assert runtime.database.get_task(task.id).scheduled_start_at is None


# ---------------------------------------------------------------- 配置与统计


def test_model_and_scope_settings(client, monkeypatch):
    headers = login(client)
    saved = client.put(
        "/api/settings/models",
        json={"default_model": "m1", "models": [{"name": "m1", "repositories": ["demo"]}, {"name": "m2"}]},
        headers=headers,
    ).json()
    assert [m["name"] for m in saved["models"]] == ["m1", "m2"]
    assert client.get("/api/settings/models").json()["default_model"] == saved["models"][0]["id"]
    bad = client.put("/api/settings/models", json={"models": [{"name": "a", "base_url": "ftp://x"}]}, headers=headers)
    assert bad.status_code == 422

    from app.api import settings as settings_api

    monkeypatch.setattr(settings_api.chat, "list_models", lambda gateway: ["x", "y"])
    found = client.post("/api/settings/llm/models", json={"base_url": "http://gw/v1", "api_key": "k"}, headers=headers)
    assert found.json()["models"] == ["x", "y"]

    scope = client.put(
        "/api/settings/review-scope", json={"rules": [{"repository": "demo", "target_branches": ["main"]}]}, headers=headers
    )
    assert scope.json()["rules"][0]["target_branches"] == ["main"]
    assert client.get("/api/settings/review-scope").json() == scope.json()


def test_health_tokens_and_index(client, runtime):
    health = client.get("/health").json()
    assert health["status"] == "ok" and health["queue_size"] == 0 and health["publisher_alive"] is False
    task = create_task(runtime.database)
    runtime.database.complete_review(task.id, [], stats={"token_input": 10, "token_output": 5})
    tokens = client.get("/api/stats/tokens", params={"limit": 5}).json()
    assert tokens["summary"]["total_tokens"] == 15 and len(tokens["items"]) == 1
    index = client.get("/")
    assert index.status_code == 200 and "/static/app.js?v=" in index.text and "/static/style.css?v=" in index.text
    assert index.headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/health").headers["X-Request-ID"]


def test_admin_disabled_without_credentials(settings):
    settings.admin_username = settings.admin_password = ""
    with TestClient(create_app(settings, start_workers=False)) as client:
        assert client.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 403


def test_create_app_rejects_half_admin_config(settings):
    settings.admin_password = ""
    with pytest.raises(ValueError):
        create_app(settings, start_workers=False)


# ---------------------------------------------------------------- 运行时（真实工作线程）


def test_runtime_end_to_end(settings, tmp_path, monkeypatch):
    repo, target, source = feature_repo(tmp_path)
    finding = {"new_path": "app.py", "new_line": 3, "severity": "major", "title": "空指针", "body": "b"}
    fake_codex(tmp_path / "bin", [{"summary": "", "findings": [finding]}], monkeypatch=monkeypatch)
    runtime = ReviewRuntime(settings)
    runtime.start()
    try:
        task = runtime.database.create_task(**{
            "project_id": 1, "mr_iid": 1, "repository_url": str(repo), "repository_name": "demo",
            "source_branch": "feature/x", "target_branch": "main", "source_sha": source, "target_sha": target,
            "action": "update",
        })
        runtime.enqueue(task)
        deadline = time.time() + 30
        while time.time() < deadline and runtime.database.get_task(task.id).status in {"pending", "running"}:
            time.sleep(0.1)
        stored = runtime.database.get_task(task.id)
        assert stored.status == "success" and stored.finding_count == 1
        assert len(codex_calls(tmp_path / "bin")) == 1
        # CodeHub 未配置：评论记录为失败（配置错误不重试）
        deadline = time.time() + 10
        while time.time() < deadline and runtime.database.list_findings(task.id)[0].status == "pending":
            time.sleep(0.1)
        assert runtime.database.list_findings(task.id)[0].status == "failed"
    finally:
        runtime.stop()
    assert not runtime.publisher.is_alive()


# ---------------------------------------------------------------- 本地提交检视（post-commit 钩子）


def test_local_review_endpoint(client, runtime, tmp_path):
    repo, target, source = feature_repo(tmp_path)
    body = {"path": str(repo)}
    # TestClient 不是回环地址：未登录时拒绝，防止借此读取服务器上的任意目录
    assert client.post("/api/local-reviews", json=body).status_code == 403
    headers = login(client)
    response = client.post("/api/local-reviews", json=body, headers=headers)
    assert response.status_code == 202
    task = runtime.database.get_task(response.json()["task_id"])
    assert (task.origin, task.source_sha, task.target_sha, task.repository_name) == ("local", source, target, "demo")
    assert task.title == "change" and task.action == "commit"
    assert runtime.queue_size() == 1

    duplicate = client.post("/api/local-reviews", json=body, headers=headers).json()
    assert duplicate["status"] == "duplicate"
    root = client.post("/api/local-reviews", json={"path": str(repo), "sha": target}, headers=headers).json()
    assert root["status"] == "ignored"
    assert client.post("/api/local-reviews", json={"path": "relative"}, headers=headers).status_code == 422


def test_local_review_allows_loopback(client, runtime, tmp_path, monkeypatch):
    from app.api import local

    repo, _, _ = feature_repo(tmp_path)
    monkeypatch.setattr(local, "LOOPBACK_HOSTS", {"testclient"})
    assert client.post("/api/local-reviews", json={"path": str(repo)}).status_code == 202


def test_installed_git_hook_posts_commit(tmp_path):
    """安装脚本生成的钩子在提交后发出请求（用假 curl 记录参数）。"""
    import os
    import subprocess
    from pathlib import Path

    repo, _, _ = feature_repo(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "install-git-hook.sh"
    subprocess.run([str(script), str(repo)], check=True, capture_output=True)
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log = tmp_path / "curl.log"
    (bin_dir / "curl").write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
    (bin_dir / "curl").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "CODE_REVIEW_URL": "http://svc:1"}
    (repo / "more.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "more"], cwd=repo, check=True, env=env)
    deadline = time.time() + 5
    while time.time() < deadline and not log.exists():
        time.sleep(0.05)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    sent = log.read_text()
    assert "http://svc:1/api/local-reviews" in sent and head in sent and str(repo) in sent
    # 重复安装是幂等的
    subprocess.run([str(script), str(repo)], check=True, capture_output=True)
