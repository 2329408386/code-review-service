"""CodeHub：OpenAPI 客户端、回写线程、MR 评价、回复分类。"""
from __future__ import annotations

import json

import httpx
import pytest
from helpers import candidate, create_task, make_task

from app.codehub.client import CodeHubClient, CodeHubError
from app.codehub.evaluation import MAX_CHARS, build_content, fallback_content, rate_mr_quality
from app.codehub.publisher import COMMENT, EVALUATION, Publisher
from app.codehub.replies import classify_reply, should_auto_resolve
from app.models import FindingRecord
from app.storage import utc_now


def _finding(**overrides) -> FindingRecord:
    data = {
        "id": 1, "review_task_id": "t", "new_path": "app.py", "old_path": "app.py", "new_line": 2,
        "severity": "major", "title": "空指针", "body": "可能为空", "status": "pending", "created_at": utc_now(),
    }
    data.update(overrides)
    return FindingRecord(**data)


# ---------------------------------------------------------------- 客户端


def _client(settings, handler) -> tuple[CodeHubClient, list[httpx.Request]]:
    settings.codehub_private_token = "token"
    settings.codehub_app_code = "app"
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = CodeHubClient(settings)
    client._http = httpx.Client(transport=httpx.MockTransport(record))
    return client, requests


def test_post_discussion_positions(settings):
    client, requests = _client(settings, lambda r: httpx.Response(201, json={"id": "disc-9"}))
    task = make_task()
    assert client.post_discussion(task, _finding()) == "disc-9"
    client.post_discussion(task, _finding(new_path="/dev/null", old_path="gone.py", new_line=5))
    client.post_discussion(task, _finding(new_path="new.py", old_path="/dev/null"))

    modified, deleted, added = (json.loads(r.content) for r in requests)
    assert requests[0].url.path.endswith("/projects/7/merge_requests/3/discussions")
    assert requests[0].headers["private-token"] == "token"
    assert modified["line_types"] == "new" and modified["position"]["new_line"] == 2
    assert modified["body"] == "【严重】空指针：可能为空" and modified["assignee_id"] == 11
    assert deleted["line_types"] == "old" and deleted["position"]["old_line"] == 5
    assert deleted["position"]["new_path"] == "gone.py"
    assert added["position"]["old_path"] == "new.py"


def test_client_error_classification(settings):
    for status, kind, retryable in ((429, "rate_limit", True), (502, "server_error", True), (400, "http_4xx", False)):
        client, _ = _client(settings, lambda r, s=status: httpx.Response(s, text="bad"))
        with pytest.raises(CodeHubError) as info:
            client.post_evaluation(make_task())
        assert (info.value.kind, info.value.retryable, info.value.status_code) == (kind, retryable, status)

    unconfigured = CodeHubClient(settings.model_copy(update={"codehub_private_token": ""}))
    with pytest.raises(CodeHubError, match="configuration"):
        unconfigured.resolve_discussion(1, 1, "d")


def test_group_credentials_override_default(settings):
    from app.codehub.client import Credentials

    client, requests = _client(settings, lambda r: httpx.Response(201, json={"id": 7}))
    client.resolver = lambda repo: Credentials("group-token", "group-app") if repo == "core" else None
    client.post_discussion(make_task(repository_name="core"), _finding())
    client.post_evaluation(make_task(repository_name="other"))
    client.resolve_discussion(1, 1, "d", "core")
    assert [r.headers["private-token"] for r in requests] == ["group-token", "token", "group-token"]
    assert requests[0].headers["X-Apig-AppCode"] == "group-app"
    # 每套凭据各自查询一次所属用户
    assert client.current_user_id("core") == 7 and client.current_user_id("other") == 7
    assert client.current_user_id("core") == 7 and len(requests) == 5


def test_current_user_cached(settings):
    client, requests = _client(settings, lambda r: httpx.Response(200, json={"id": 42}))
    assert client.current_user_id() == 42 and client.current_user_id() == 42
    assert len(requests) == 1


# ---------------------------------------------------------------- 回写线程


class FakeClient:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.calls: list[tuple] = []

    def _maybe_fail(self):
        if self.errors:
            raise self.errors.pop(0)

    def post_discussion(self, task, finding):
        self.calls.append(("comment", finding.id))
        self._maybe_fail()
        return f"disc-{finding.id}"

    def post_evaluation(self, task):
        self.calls.append(("evaluation", task.id))
        self._maybe_fail()


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _publisher(settings, database, client):
    clock = FakeClock()
    return Publisher(database, client, settings, clock=clock, sleep=clock.sleep), clock


def test_publisher_sends_with_rate_limit(settings, database):
    task = create_task(database)
    ids = database.complete_review(task.id, [candidate(line=2), candidate(line=3, title="b")], stats={})
    client = FakeClient()
    publisher, clock = _publisher(settings, database, client)
    for finding_id in ids:
        publisher.process(COMMENT, finding_id)
    assert [f.status for f in database.list_findings(task.id)] == ["sent", "sent"]
    assert database.list_findings(task.id)[0].discussion_id == f"disc-{ids[0]}"
    assert clock.sleeps == [pytest.approx(5.1)]  # 两次请求间隔至少 5 秒
    publisher.process(COMMENT, ids[0])  # 已发送的不再重复发送
    assert len(client.calls) == 2


def test_publisher_retries_and_fails(settings, database):
    task = create_task(database)
    [finding_id] = database.complete_review(task.id, [candidate()], stats={})
    retryable = CodeHubError("x", kind="server_error", retryable=True)
    client = FakeClient([retryable, retryable, retryable])
    publisher, _ = _publisher(settings, database, client)
    publisher.process(COMMENT, finding_id)
    finding = database.list_findings(task.id)[0]
    assert finding.status == "failed" and finding.comment_retry_count == 3

    # 不可重试的错误只尝试一次
    task2 = create_task(database, source_sha="c" * 40)
    [other] = database.complete_review(task2.id, [candidate(line=9)], stats={})
    client2 = FakeClient([CodeHubError("bad", kind="http_4xx", retryable=False)])
    _publisher(settings, database, client2)[0].process(COMMENT, other)
    assert len(client2.calls) == 1


def test_publisher_skips_duplicate_and_sends_evaluation(settings, database):
    first = create_task(database, source_sha="1" * 40)
    [sent] = database.complete_review(first.id, [candidate()], stats={})
    database.mark_comment_sent(sent, "d")
    second = create_task(database, source_sha="2" * 40)
    [dup] = database.complete_review(second.id, [candidate()], stats={})
    database.queue_evaluation(second.id, level=3, content="评价")
    client = FakeClient()
    publisher, _ = _publisher(settings, database, client)
    publisher.process(COMMENT, dup)
    publisher.process(EVALUATION, second.id)
    assert database.list_findings(second.id)[0].status == "skipped"
    assert client.calls == [("evaluation", second.id)]
    assert database.get_task(second.id).evaluation_status == "sent"


def test_publisher_thread_lifecycle(settings, database):
    task = create_task(database)
    [finding_id] = database.complete_review(task.id, [candidate()], stats={})
    publisher = Publisher(database, FakeClient(), settings, sleep=lambda _: None)
    publisher.start()
    publisher.enqueue_comment(finding_id)
    publisher.stop()
    assert not publisher.is_alive()
    assert database.list_findings(task.id)[0].status == "sent"


# ---------------------------------------------------------------- 评价与回复


def test_rate_and_content():
    assert rate_mr_quality([]) == 5
    assert rate_mr_quality([_finding(severity="major")] * 4) == 1
    task = make_task(reviewed_file_count=1, reviewed_added_line_count=10)
    content = fallback_content(task, [_finding()])
    assert content.startswith("**检视思路**") and "\n**总体评价**" in content and len(content) <= MAX_CHARS
    assert "无新增代码变更" in fallback_content(make_task(), [])
    # 模型把两点写在同一行时自动断开；超长截断
    assert build_content(task, [], "1. 检视思路：看了 2. 总体评价：好") == "1. 检视思路：看了\n2. 总体评价：好"
    assert len(build_content(task, [], "很长" * 200)) == MAX_CHARS
    assert build_content(task, [], None) == fallback_content(task, [])


def test_reply_classification():
    assert classify_reply("确认没问题") == "no_problem"
    assert classify_reply("已解决") == "resolved"
    assert classify_reply("无风险") == "no_risk"
    assert classify_reply("稍后看") == "other"
    assert should_auto_resolve("确认") and not should_auto_resolve("稍后看")
