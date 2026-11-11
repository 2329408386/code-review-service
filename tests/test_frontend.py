"""Web 控制台静态资源：页面元素与脚本保持一致，关键接口调用存在。"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")


def test_every_script_element_id_exists_in_html():
    html_ids = set(re.findall(r'id="([^"]+)"', HTML))
    script_ids = set(re.findall(r'\$\("([^"]+)"\)', JS)) | set(re.findall(r"\$\(`page-\$\{[^}]+\}`\)", JS))
    script_ids = {item for item in script_ids if not item.startswith("$")}
    missing = sorted(item for item in script_ids if item not in html_ids)
    assert not missing, f"app.js 引用了 index.html 中不存在的元素：{missing}"
    for page in ("reviews", "stats", "models", "scope"):
        assert f'id="page-{page}"' in HTML and f'href="#{page}"' in HTML


def test_script_calls_backend_apis():
    for path in (
        "/api/reviews?",
        "/api/reviews/${encodeURIComponent(taskId)}",
        "/findings/export",
        "/events?reasoning_only=true",
        "/api/review-events/stream?task_id=",
        "/api/stats/review-replies",
        "/api/stats/tokens?limit=200",
        "/api/settings/models",
        "/api/settings/llm/models",
        "/api/settings/review-scope",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/status",
        "/run-now",
        "/rerun",
        '"/health"',
    ):
        assert path in JS, path
    assert 'addEventListener("review_event"' in JS
    assert "Authorization = `Bearer ${token}`" in JS


def test_script_behaviour_markers():
    assert '"晚上8点后检视"' in JS  # 延后任务单独分组
    # 代码仓、提交人、状态、排序、展示方式、每页数量都保存在浏览器本地，刷新后恢复
    assert "localStorage.setItem(FILTERS_KEY" in JS and "restoreFilters();" in JS
    for field in ("filter-repository", "filter-author", "filter-sort", "filter-view", "filter-page-size"):
        assert f'"{field}"' in JS
    # 记录以小卡片网格展示，默认每页 30 条
    assert 'class="review-grid"' in JS and ".review-grid" in CSS
    assert '<option value="30" selected>' in HTML
    assert "function escapeHtml" in JS
    assert re.search(r"\^codex\\\.\(discovery\|verify\|direct\)", JS)  # 推理过程按分片分组
    # 未登录时配置页只读
    assert '$("model-readonly").hidden = editable' in JS
    assert '$("scope-readonly").hidden = editable' in JS


def test_styles_support_dark_mode_and_mobile():
    assert "prefers-color-scheme: dark" in CSS
    assert "@media (max-width: 820px)" in CSS
    for status in ("pending", "running", "success", "failed", "ignored"):
        assert f".status-{status}" in CSS


def test_local_commit_display():
    # 本地提交显示短提交号与“仅平台展示”，不显示 MR 编号
    assert 'task.origin === "local"' in JS
    assert 'local: "仅平台展示"' in JS
