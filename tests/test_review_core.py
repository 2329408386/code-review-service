"""检视核心纯逻辑：Diff 解析与分片、意见校验与合并、提示词、检视范围、模型输出解析。"""
from __future__ import annotations

import json

import pytest
from helpers import candidate, make_task

from app.llm.output import agent_message, parse_review_output, progress_from_event, repair_json_text, token_usage
from app.models import MergeRequestAttributes
from app.review.diff import (
    DEV_NULL,
    chunk_diff,
    filter_reviewable,
    is_reviewable_path,
    parse_unified_diff,
    plan_review_chunks,
)
from app.review.findings import (
    UNVERIFIED_CONFIRMATION,
    deduplicate_findings,
    merge_verification,
    validate_findings,
)
from app.review.prompts import build_evaluation_prompt, build_review_prompt, build_verification_prompt
from app.review.scope import ReviewScopeError, ReviewScopeRegistry, skip_reason

MODIFIED = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 def run():
-    return 1
+    value = None
+    return value.x
 # end
"""

ADDED = """diff --git a/new.py b/new.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+a = 1
+b = 2
"""

DELETED = """diff --git a/old.py b/old.py
deleted file mode 100644
index 4444444..0000000
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
--- y = 2
"""

BINARY = """diff --git a/logo.png b/logo.png
index 5555555..6666666 100644
Binary files a/logo.png and b/logo.png differ
"""


# ---------------------------------------------------------------- Diff


def test_parse_line_numbers_and_change_types():
    parsed = parse_unified_diff(MODIFIED + ADDED + DELETED + BINARY)
    modified, added, deleted, binary = parsed.files
    assert modified.added_lines == {2, 3} and modified.deleted_lines == {2}
    assert modified.commentable_new_lines == {1, 2, 3, 4}
    assert added.change_type == "added" and added.old_path == DEV_NULL and added.added_lines == {1, 2}
    # 删除文件中以 "--- " 开头的正文行不能被误当成文件头
    assert deleted.is_deleted and deleted.path == "old.py" and deleted.deleted_lines == {1, 2}
    assert binary.is_binary
    assert [file.path for file in parsed.reviewable_files] == ["app.py", "new.py", "old.py"]


def test_filter_reviewable_paths():
    assert not is_reviewable_path("package-lock.json", ())
    assert not is_reviewable_path("web/a.min.js", ("*.min.js",))
    assert is_reviewable_path("src/a.py", ("*.min.js",))
    parsed = filter_reviewable(parse_unified_diff(MODIFIED + BINARY), ("*.md",))
    assert [file.path for file in parsed.files] == ["app.py"]


def test_chunk_diff_splits_large_hunks_and_keeps_line_numbers():
    body = "".join(f"+line {index} " + "x" * 80 + "\n" for index in range(1, 401))
    diff = f"diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -0,0 +1,400 @@\n{body}"
    parsed = parse_unified_diff(diff)
    chunks = chunk_diff(parsed, 10_000)
    assert len(chunks) > 1
    assert all(len(chunk.prompt_text().encode()) <= 10_000 for chunk in chunks)
    lines = set().union(*(chunk.files[0].added_lines for chunk in chunks))
    assert lines == set(range(1, 401))
    with pytest.raises(ValueError):
        chunk_diff(parsed, 0)


def test_plan_review_chunks_direct_for_large_new_files():
    parsed = parse_unified_diff(MODIFIED + ADDED)
    chunks = plan_review_chunks(parsed, 100_000, large_new_file_lines=1)
    assert [chunk.direct_llm for chunk in chunks] == [False, True]
    assert chunks[1].diff.files[0].path == "new.py"
    assert len(plan_review_chunks(parsed, 100_000, large_new_file_lines=5)) == 1


# ---------------------------------------------------------------- 意见校验


def test_validate_findings_positions():
    diff = parse_unified_diff(MODIFIED + ADDED + DELETED)
    result = validate_findings(
        [
            candidate("app.py", 2),
            candidate("./app.py", 4),  # 上下文行，不允许评论
            candidate("missing.py", 1),
            candidate("new.py", 1, old_path="other.py"),  # old_path 冲突
            candidate("new.py", 2),
            candidate(DEV_NULL, 2, old_path="old.py"),  # 删除文件按旧行号
            candidate("app.py", 3, title=" "),
        ],
        diff,
    )
    assert [(f.new_path, f.new_line, f.old_path) for f in result.accepted] == [
        ("app.py", 2, "app.py"),
        ("new.py", 2, DEV_NULL),
        (DEV_NULL, 2, "old.py"),
    ]
    assert len(result.rejected) == 4


def test_deduplicate_and_merge_verification():
    a = candidate(line=2, title="空指针")
    assert deduplicate_findings([a, candidate(line=2, title=" 空指针 ")]) == [a]

    discovered = [candidate(line=2, title="A"), candidate(line=3, title="B"), candidate(line=3, title="C")]
    verified = [candidate(line=2, title="A 改写"), candidate(line=3, title="B")]
    merged, unverified = merge_verification(discovered, verified)
    titles = [item.title for item in merged]
    # 第 2 行一对一视为改写；第 3 行 C 未被确认，追加确认提示
    assert titles == ["A 改写", "B", "C"]
    assert unverified == 1 and merged[2].body.endswith(UNVERIFIED_CONFIRMATION)


# ---------------------------------------------------------------- 提示词


def test_prompts_include_scope_and_rules():
    diff = parse_unified_diff(MODIFIED)
    prompt = build_review_prompt(make_task(), diff)
    assert "app.py" in prompt and "+    value = None" in prompt
    verify = build_verification_prompt(make_task(), diff, [candidate()])
    assert "空指针" in verify
    evaluation = build_evaluation_prompt(make_task(), [])
    assert "总体评价" in evaluation


# ---------------------------------------------------------------- 检视范围


def _attrs(**overrides) -> MergeRequestAttributes:
    data = {
        "iid": 1, "state": "opened", "action": "open", "source_branch": "feature/x", "target_branch": "main",
        "last_commit": {"id": "a" * 40}, "source": {"git_ssh_url": "git@h:g/demo.git"},
    }
    data.update(overrides)
    return MergeRequestAttributes.model_validate(data)


def test_scope_registry_and_skip_reason(settings, database):
    scope = ReviewScopeRegistry(database)
    assert skip_reason(_attrs(), "demo", settings, scope) is None
    assert "state" in skip_reason(_attrs(state="merged"), "demo", settings, scope)
    assert "work in progress" in skip_reason(_attrs(work_in_progress=True), "demo", settings, scope)
    assert "action" in skip_reason(_attrs(action="close"), "demo", settings, scope)
    assert "全局跳过" in skip_reason(_attrs(source_branch="develop"), "demo", settings, scope)

    scope.replace([{"repository": "demo", "target_branches": ["release", "release"]}])
    assert scope.rules() == [{"repository": "demo", "target_branches": ["release"]}]
    assert "不在允许列表" in skip_reason(_attrs(), "demo", settings, scope)
    assert skip_reason(_attrs(target_branch="release"), "demo", settings, scope) is None
    # 持久化：新实例读取同一配置
    assert ReviewScopeRegistry(database).allowed_branches("demo") == ("release",)
    with pytest.raises(ReviewScopeError):
        scope.replace([{"repository": "demo", "target_branches": []}])


# ---------------------------------------------------------------- 模型输出解析


def test_parse_review_output_variants():
    plain = {"summary": "ok", "findings": [{"new_path": "a.py", "new_line": 1, "severity": "major", "title": "t", "body": "b"}]}
    assert parse_review_output(json.dumps(plain)).findings[0].new_path == "a.py"
    fenced = "说明\n```json\n" + json.dumps(plain) + "\n```"
    assert parse_review_output(fenced).summary == "ok"
    aliased = {"result": {"summary": "", "findings": [{"file": "b.py", "line": 2, "severity": "MAJOR", "title": "t", "body": "b"}]}}
    finding = parse_review_output(json.dumps(aliased)).findings[0]
    assert (finding.new_path, finding.new_line, finding.severity) == ("b.py", 2, "major")
    assert parse_review_output('[]').findings == []
    assert parse_review_output('{"summary": "x\n", "findings": [],}').summary == "x"
    with pytest.raises(ValueError):
        parse_review_output("没有 JSON")


def test_repair_json_text():
    assert json.loads(repair_json_text('{"a": "x\ny", "b": [1,],}')) == {"a": "x\ny", "b": [1]}


def test_event_helpers():
    event = {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2, "cached_input_tokens": 1}}
    assert token_usage(event) == (5, 2, 1)
    reasoning = {"type": "item.completed", "item": {"type": "reasoning", "text": "思考"}}
    assert progress_from_event(reasoning).message == "推理摘要"
    final = {"type": "item.completed", "item": {"type": "agent_message", "text": '{"findings": []}'}}
    assert progress_from_event(final) is None
    assert agent_message(final) == '{"findings": []}'


# ---------------------------------------------------------------- CodeGraph（可选，失败只降级）


def test_codegraph_indexer(settings, tmp_path):
    from app.review.codegraph import CodeGraphIndexer

    settings.codegraph_command = str(tmp_path / "missing-codegraph")
    missing = CodeGraphIndexer(settings)
    assert not missing.installed and missing.version is None
    assert not missing.build_index(tmp_path).success

    script = tmp_path / "codegraph"
    script.write_text('#!/bin/sh\n[ "$1" = "--version" ] && echo cg-1.0 && exit 0\n[ -f fail ] && exit 2\nmkdir -p .codegraph\n')
    script.chmod(0o755)
    settings.codegraph_command = str(script)
    indexer = CodeGraphIndexer(settings)
    assert indexer.version == "cg-1.0"
    assert indexer.build_index(tmp_path).success and (tmp_path / ".codegraph").is_dir()
    (tmp_path / "fail").write_text("")
    result = indexer.build_index(tmp_path)
    assert not result.success and "exit 2" in result.error
