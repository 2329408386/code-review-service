"""使用文档：读取仓库 docs/ 下的 Markdown，供 Web 控制台“使用说明”页面渲染（公开只读）。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/api/docs", tags=["docs"])

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
# 页面上展示的文档（顺序即标签页顺序）；只允许读取这里列出的文件
DOCUMENTS = (
    {"key": "guide", "title": "使用说明", "file": "使用说明.md"},
    {"key": "onboarding", "title": "接入指南", "file": "接入指南.md"},
)


@router.get("")
def list_docs() -> list[dict]:
    return [{"key": doc["key"], "title": doc["title"]} for doc in DOCUMENTS]


@router.get("/{key}")
def read_doc(key: str) -> dict:
    doc = next((item for item in DOCUMENTS if item["key"] == key), None)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    try:
        content = (DOCS_DIR / doc["file"]).read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="文档不存在") from exc
    return {"key": doc["key"], "title": doc["title"], "markdown": content}
