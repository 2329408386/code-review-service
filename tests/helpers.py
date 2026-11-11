"""测试公共工具：构造任务 / 意见、本地 Git 仓库、伪造的 Codex CLI。"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from app.models import FindingCandidate, ReviewTask
from app.storage import Database, utc_now

SHA_A = "a" * 40
SHA_B = "b" * 40


def task_fields(**overrides) -> dict:
    """create_task 所需的最小字段集合。"""
    fields = {
        "project_id": 7,
        "mr_iid": 3,
        "repository_url": "git@example.com:group/demo.git",
        "repository_name": "demo",
        "source_branch": "feature/x",
        "target_branch": "main",
        "source_sha": SHA_A,
        "target_sha": SHA_B,
        "action": "open",
        "title": "新增功能",
        "author": "alice",
        "assignee_id": 11,
    }
    fields.update(overrides)
    return fields


def make_task(**overrides) -> ReviewTask:
    """不落库的 ReviewTask（用于纯函数测试）。"""
    return ReviewTask(id=overrides.pop("id", "task-1"), created_at=utc_now(), **task_fields(**overrides))


def create_task(database: Database, **overrides) -> ReviewTask:
    status = overrides.pop("status", "pending")
    return database.create_task(status=status, **task_fields(**overrides))


def candidate(path: str = "app.py", line: int = 2, title: str = "空指针", **overrides) -> FindingCandidate:
    data = {"new_path": path, "new_line": line, "severity": "major", "title": title, "body": "可能为空"}
    data.update(overrides)
    return FindingCandidate(**data)


# ---------------------------------------------------------------- Git


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test")
    return path


def commit_all(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def feature_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """main 上有 app.py；feature 分支修改 app.py。返回 (仓库, target_sha, source_sha)。"""
    repo = init_repo(tmp_path / "demo")
    (repo / "app.py").write_text("def run():\n    return 1\n")
    target = commit_all(repo, "base")
    git(repo, "checkout", "-q", "-b", "feature/x")
    (repo / "app.py").write_text("def run():\n    value = None\n    return value.x\n")
    source = commit_all(repo, "change")
    return repo, target, source


# ---------------------------------------------------------------- 伪造 Codex CLI


def fake_codex(
    directory: Path, outputs: list[dict | str], *, monkeypatch=None, exit_code: int = 0, usage: tuple = (100, 20, 5)
) -> Path:
    """生成一个伪 codex 可执行文件：按调用次数依次输出 outputs 中的结论，并打印 JSONL 事件。

    第 N 次调用使用 outputs[min(N, len-1)]；调用参数追加写入 calls.jsonl 便于断言。
    传入 monkeypatch 时把 directory 放到 PATH 最前面，使 CodexClient 找到这个伪命令。
    """
    directory.mkdir(parents=True, exist_ok=True)
    payloads = [item if isinstance(item, str) else json.dumps(item, ensure_ascii=False) for item in outputs]
    script = directory / "codex"
    script.write_text(
        f"""#!/usr/bin/env python3
import json, os, sys
directory = {str(directory)!r}
payloads = {payloads!r}
if sys.argv[1:] == ["--version"]:
    print("codex-cli 9.9.9"); sys.exit(0)
counter = os.path.join(directory, "count")
count = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(count + 1))
prompt = sys.stdin.read()
with open(os.path.join(directory, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "prompt": prompt, "key": os.environ.get("CODE_REVIEW_LLM_API_KEY")}}) + "\\n")
args = sys.argv[1:]
output = args[args.index("--output-last-message") + 1]
print(json.dumps({{"type": "item.completed", "item": {{"type": "reasoning", "text": "正在阅读 app.py"}}}}), flush=True)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": {usage[0]}, "output_tokens": {usage[1]}, "cached_input_tokens": {usage[2]}}}}}), flush=True)
open(output, "w").write(payloads[min(count, len(payloads) - 1)])
if {exit_code}:
    sys.stderr.write("boom"); sys.exit({exit_code})
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    if monkeypatch is not None:
        monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return script


def codex_calls(directory: Path) -> list[dict]:
    path = directory / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
