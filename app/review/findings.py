"""检视意见的确定性质量门禁：定位校验、去重。"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import FindingCandidate
from .diff import DEV_NULL, DiffFile, ParsedDiff, normalize_path

@dataclass(frozen=True)
class ValidationResult:
    accepted: list[FindingCandidate]
    rejected: list[str]


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip()).lower()


def finding_key(finding: FindingCandidate) -> tuple[str, int, str]:
    """意见身份：路径 + 行号 + 归一化标题（正文措辞每轮都会变化，不参与比较）。"""
    return normalize_path(finding.new_path), finding.new_line, normalize_title(finding.title)


def validate_findings(candidates: list[FindingCandidate], diff: ParsedDiff) -> ValidationResult:
    """以 Diff 为准校验模型给出的位置：只接受新增行；删除文件上的意见按旧行号接受。"""
    by_new_path = {file.new_path: file for file in diff.files if not file.is_deleted}
    deleted_by_old_path = {normalize_path(file.old_path): file for file in diff.files if file.is_deleted}
    accepted: list[FindingCandidate] = []
    rejected: list[str] = []
    for index, candidate in enumerate(candidates):
        path = normalize_path(candidate.new_path)
        old_path = normalize_path(candidate.old_path) if candidate.old_path else None
        file: DiffFile | None = by_new_path.get(path)
        is_deleted = False
        if file is None:
            # 模型可能照抄删除文件的 /dev/null 新路径，此时用旧路径查找
            file = deleted_by_old_path.get(old_path if path == DEV_NULL and old_path else path)
            is_deleted = file is not None
        if file is None:
            rejected.append(f"#{index}: new_path is not in diff")
            continue
        # 新文件的 old_path 模型往往不知道：只拒绝明确冲突的值，最终以 Diff 为准回填
        if old_path is not None and old_path != normalize_path(file.old_path):
            rejected.append(f"#{index}: old_path does not match diff")
            continue
        valid_lines = file.deleted_lines if is_deleted else file.added_lines
        if candidate.new_line not in valid_lines:
            rejected.append(f"#{index}: new_line is not a commentable diff position")
            continue
        if not candidate.title.strip() or not candidate.body.strip():
            rejected.append(f"#{index}: title/body cannot be empty")
            continue
        accepted.append(
            candidate.model_copy(
                update={
                    # 删除行没有新侧：new_path 记为 /dev/null，评论时改用旧行定位
                    "new_path": DEV_NULL if is_deleted else path,
                    "old_path": normalize_path(file.old_path),
                    "title": candidate.title.strip(),
                    "body": candidate.body.strip(),
                }
            )
        )
    return ValidationResult(accepted=accepted, rejected=rejected)


def deduplicate_findings(findings: list[FindingCandidate]) -> list[FindingCandidate]:
    seen: set[tuple[str, int, str]] = set()
    result: list[FindingCandidate] = []
    for finding in findings:
        key = finding_key(finding)
        if key not in seen:
            seen.add(key)
            result.append(finding)
    return result
