"""端到端冒烟脚本（不属于 pytest 用例）：本地仓库 + 真实 Codex CLI + 真实大模型网关。

用法：
    .venv/bin/python tests/e2e_smoke.py [env 文件 ...]

默认依次读取 .env.example 与 .env.local（后者已 gitignore，存放 OPENAI_API_KEY 等密钥，覆盖前者）。

脚本会创建一个含明显缺陷的临时 Git 仓库，通过“本地提交检视”接口提交最新一次提交，
等待任务完成后打印检视意见与整体评价（本地提交只在平台展示，不回写 CodeHub）。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import dotenv_values
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402

BASE = "def average(values):\n    total = 0\n    for value in values:\n        total += value\n    return total / len(values)\n"
CHANGED = (
    "def average(values):\n    total = 0\n    for value in values:\n        total += value\n"
    "    return total / len(values)\n\n\n"
    "def read_config(path):\n    file = open(path)\n    data = file.read()\n    return eval(data)\n\n\n"
    "def first_item(items):\n    return items[1]\n"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True).stdout.strip()


def main() -> None:
    env_files = [Path(arg) for arg in sys.argv[1:]] or [ROOT / ".env.example", ROOT / ".env.local"]
    env: dict[str, str] = {}
    for env_file in env_files:
        if env_file.exists():
            env.update({key.lower(): value for key, value in dotenv_values(env_file).items() if value})
    work = Path(tempfile.mkdtemp(prefix="review-e2e-"))
    repo = work / "demo-repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "e2e@example.com")
    git(repo, "config", "user.name", "e2e")
    (repo / "utils.py").write_text(BASE)
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    git(repo, "checkout", "-b", "feature/demo")
    (repo / "utils.py").write_text(CHANGED)
    git(repo, "commit", "-am", "add helpers")
    source_sha = git(repo, "rev-parse", "HEAD")

    keep = ("openai_api_base_url", "openai_api_key", "openai_model")
    settings = Settings(
        _env_file=None,
        data_dir=work / "data",
        log_dir=work / "log",
        admin_username="e2e",
        admin_password="e2e",
        **{key: env[key] for key in keep if key in env},
    )
    with TestClient(create_app(settings)) as client:
        # TestClient 不是本机回环地址，用管理员身份提交本地检视（与 post-commit 钩子走同一接口）
        token = client.post("/api/auth/login", json={"username": "e2e", "password": "e2e"}).json()["token"]
        response = client.post(
            "/api/local-reviews", json={"path": str(repo), "sha": source_sha},
            headers={"Authorization": f"Bearer {token}"},
        )
        print("submit:", response.status_code, response.json())
        task_id = response.json()["task_id"]
        deadline = time.time() + 900
        while time.time() < deadline:
            task = client.get(f"/api/reviews/{task_id}").json()["task"]
            # 整体评价在任务成功后才生成，需一并等待
            if task["status"] in {"failed", "ignored"} or (task["status"] == "success" and task["evaluation_status"]):
                break
            time.sleep(3)
        detail = client.get(f"/api/reviews/{task_id}").json()
        task = detail["task"]
        print(f"status={task['status']} findings={task['finding_count']} models={task['models_used']} "
              f"tokens={task['token_input']}/{task['token_output']} error={task['error_message']}")
        for finding in detail["findings"]:
            print(f"  [{finding['severity']}] {finding['new_path']}:{finding['new_line']} {finding['title']}")
        print("evaluation:", task["evaluation_level"], repr(task["evaluation_content"]))
        print("work dir:", work)
        sys.exit(0 if task["status"] == "success" else 1)


if __name__ == "__main__":
    main()
