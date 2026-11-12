"""使用文档：读取仓库 docs/ 下的 Markdown，供 Web 控制台“使用说明”页面渲染（公开只读）。"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/docs", tags=["docs"])

DOCS_DIR = Path(__file__).resolve().parents[2] / "docs"
IMAGES_DIR = DOCS_DIR / "images"
_IMAGE_NAME = re.compile(r"^[\w-]+\.(png|jpg|jpeg|webp|gif)$")
# 页面上展示的文档（顺序即标签页顺序）；只允许读取这里列出的文件
DOCUMENTS = (
    {"key": "guide", "title": "使用说明", "file": "使用说明.md"},
    {"key": "onboarding", "title": "接入指南", "file": "接入指南.md"},
)


@router.get("")
def list_docs() -> list[dict]:
    return [{"key": doc["key"], "title": doc["title"]} for doc in DOCUMENTS]


@router.get("/images/{name}")
def doc_image(name: str) -> FileResponse:
    """文档截图（docs/images 下，文件名白名单校验）。"""
    path = IMAGES_DIR / name
    if not _IMAGE_NAME.match(name) or not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


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
