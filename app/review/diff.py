"""统一 Diff 解析、可检视文件过滤与分片规划。

解析结果保留每个文件的原始补丁文本（用于 Prompt）和精确的行号集合（用于校验模型给出的定位）。
分片按“完整文件 → hunk → 行”逐级拆分，绝不截断任何变更行。
"""
from __future__ import annotations

import ast
import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:\s.*)?$")
DEV_NULL = "/dev/null"

_BINARY_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".pdf", ".zip", ".gz", ".tar", ".rar",
    ".7z", ".mp3", ".mp4", ".mov", ".avi", ".woff", ".woff2", ".ttf", ".so", ".dll", ".class",
)
_LOCK_FILES = {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "cargo.lock"}


def normalize_path(path: str) -> str:
    value = path.strip().replace("\\", "/")
    return value[2:] if value.startswith(("a/", "b/")) else value


def _parse_header_path(value: str, prefix: str) -> str:
    """解析 ---/+++ 行中的路径，处理 Git 对特殊字符的引号转义。"""
    value = value.split("\t", 1)[0]
    if value.startswith('"') and value.endswith('"'):
        try:
            value = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            value = value[1:-1]
    if value == DEV_NULL:
        return DEV_NULL
    return value[len(prefix):] if value.startswith(prefix) else normalize_path(value)


@dataclass
class DiffHunk:
    old_start: int
    new_start: int
    raw: str
    added_lines: set[int] = field(default_factory=set)
    deleted_lines: set[int] = field(default_factory=set)
    context_lines: set[int] = field(default_factory=set)


@dataclass
class DiffFile:
    old_path: str
    new_path: str
    raw: str
    hunks: list[DiffHunk] = field(default_factory=list)
    is_binary: bool = False
    change_type: str = "modified"  # added / deleted / renamed / modified

    @property
    def is_deleted(self) -> bool:
        return self.new_path == DEV_NULL

    @property
    def path(self) -> str:
        """文件的真实路径：删除文件用旧路径，其余用新路径。"""
        return self.old_path if self.is_deleted else self.new_path

    @property
    def added_lines(self) -> set[int]:
        return {line for hunk in self.hunks for line in hunk.added_lines}

    @property
    def deleted_lines(self) -> set[int]:
        return {line for hunk in self.hunks for line in hunk.deleted_lines}

    @property
    def commentable_new_lines(self) -> set[int]:
        return {line for hunk in self.hunks for line in hunk.added_lines | hunk.context_lines}


@dataclass
class ParsedDiff:
    files: list[DiffFile]

    @property
    def reviewable_files(self) -> list[DiffFile]:
        return [file for file in self.files if not file.is_binary]

    def prompt_text(self) -> str:
        files = self.reviewable_files
        return "".join(file.raw.rstrip("\n") + "\n" for file in files)


@dataclass(frozen=True)
class ReviewChunk:
    diff: ParsedDiff
    # 超大新增文件：整文件交给模型单轮直检，不再切分
    direct_llm: bool = False


# ---------------------------------------------------------------------------- 解析


def _parse_hunks(lines: list[str]) -> list[DiffHunk]:
    hunks: list[DiffHunk] = []
    index = 0
    while index < len(lines):
        header = lines[index].rstrip("\n")
        match = HUNK_RE.match(header)
        index += 1
        if not match:
            continue
        old_line = old_start = int(match.group(1))
        new_line = new_start = int(match.group(3))
        hunk = DiffHunk(old_start=old_start, new_start=new_start, raw=header + "\n")
        while index < len(lines):
            line = lines[index]
            if HUNK_RE.match(line.rstrip("\n")) or line.startswith("diff --git "):
                break
            hunk.raw += line
            index += 1
            # “\ No newline at end of file” 属于上一行，不移动行号
            prefix = line[:1]
            if prefix == "+":
                hunk.added_lines.add(new_line)
                new_line += 1
            elif prefix == "-":
                hunk.deleted_lines.add(old_line)
                old_line += 1
            elif prefix == " ":
                hunk.context_lines.add(new_line)
                old_line += 1
                new_line += 1
        hunks.append(hunk)
    return hunks


def _parse_file(lines: list[str]) -> DiffFile:
    old_path = new_path = ""
    change_type = "modified"
    is_binary = False
    for line in lines:
        text = line.rstrip("\n")
        if HUNK_RE.match(text):
            break
        if text.startswith("new file mode"):
            change_type = "added"
        elif text.startswith("deleted file mode"):
            change_type = "deleted"
        elif text.startswith("rename from"):
            change_type = "renamed"
        elif text.startswith(("Binary files ", "GIT binary patch")):
            is_binary = True
        elif text.startswith("--- "):
            old_path = _parse_header_path(text[4:], "a/")
        elif text.startswith("+++ "):
            new_path = _parse_header_path(text[4:], "b/")
    if change_type == "modified" and old_path == DEV_NULL:
        change_type = "added"
    if change_type == "modified" and new_path == DEV_NULL:
        change_type = "deleted"
    if not old_path or not new_path:
        # 二进制等无 ---/+++ 行的补丁：从 diff --git 行恢复路径，仅用于展示
        match = re.match(r"diff --git a/(.+) b/(.+)$", lines[0].rstrip("\n"))
        if match:
            old_path = old_path or normalize_path(match.group(1))
            new_path = new_path or normalize_path(match.group(2))
    hunks = _parse_hunks(lines)
    return DiffFile(
        old_path=old_path,
        new_path=new_path,
        raw="".join(lines),
        hunks=hunks,
        is_binary=is_binary or not hunks,
        change_type=change_type,
    )


def parse_unified_diff(text: str) -> ParsedDiff:
    lines = text.splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --git ")]
    bounds = zip(starts, [*starts[1:], len(lines)])
    return ParsedDiff(files=[_parse_file(lines[start:end]) for start, end in bounds])


# ---------------------------------------------------------------------------- 过滤


def is_reviewable_path(path: str, patterns: tuple[str, ...]) -> bool:
    if not path or path == DEV_NULL:
        return False
    normalized = normalize_path(path)
    name = PurePosixPath(normalized).name
    if normalized.lower().endswith(_BINARY_SUFFIXES) or name.lower() in _LOCK_FILES:
        return False
    return not any(fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(name, pattern) for pattern in patterns)


def filter_reviewable(parsed: ParsedDiff, patterns: tuple[str, ...]) -> ParsedDiff:
    """去掉二进制、锁文件和匹配跳过模式的文件；删除文件按其真实旧路径判断。"""
    return ParsedDiff(
        files=[file for file in parsed.files if not file.is_binary and is_reviewable_path(file.path, patterns)]
    )


# ---------------------------------------------------------------------------- 分片


def _file_header(file: DiffFile) -> str:
    header: list[str] = []
    for line in file.raw.splitlines(keepends=True):
        if HUNK_RE.match(line.rstrip("\n")):
            break
        header.append(line)
    return "".join(header)


def _split_oversized_hunk(file: DiffFile, header: str, hunk: DiffHunk, max_bytes: int) -> list[DiffFile]:
    """单个 hunk 超过预算时按行拆分，并重新计算每段的起始行号，保证行号映射仍然正确。"""
    logical: list[str] = []
    for line in hunk.raw.splitlines(keepends=True)[1:]:
        if line.startswith("\\ No newline at end of file") and logical:
            logical[-1] += line
        else:
            logical.append(line)
    budget = max(1, max_bytes - len(header.encode()) - 100)
    groups: list[list[str]] = [[]]
    size = 0
    for line in logical:
        line_size = len(line.encode())
        if groups[-1] and size + line_size > budget:
            groups.append([])
            size = 0
        groups[-1].append(line)
        size += line_size

    parts: list[DiffFile] = []
    old_cursor, new_cursor = hunk.old_start, hunk.new_start
    for group in groups:
        old_count = sum(1 for line in group if line[:1] in "- ")
        new_count = sum(1 for line in group if line[:1] in "+ ")
        synthetic = header + f"@@ -{old_cursor},{old_count} +{new_cursor},{new_count} @@\n" + "".join(group)
        old_cursor += old_count
        new_cursor += new_count
        parts.extend(parse_unified_diff(synthetic).files)
    return parts


def _file_units(file: DiffFile, max_bytes: int) -> list[DiffFile]:
    """把一个文件拆成不超过预算的若干单元（优先按 hunk 分组）。"""
    if len(file.raw.encode()) <= max_bytes:
        return [file]
    header = _file_header(file)
    header_bytes = len(header.encode())

    def unit(hunks: list[DiffHunk]) -> DiffFile:
        return DiffFile(
            old_path=file.old_path,
            new_path=file.new_path,
            raw=header + "".join(hunk.raw for hunk in hunks),
            hunks=hunks,
            change_type=file.change_type,
        )

    units: list[DiffFile] = []
    current: list[DiffHunk] = []
    current_bytes = header_bytes
    for hunk in file.hunks:
        hunk_bytes = len(hunk.raw.encode())
        if header_bytes + hunk_bytes > max_bytes:
            if current:
                units.append(unit(current))
                current, current_bytes = [], header_bytes
            units.extend(_split_oversized_hunk(file, header, hunk, max_bytes))
            continue
        if current and current_bytes + hunk_bytes > max_bytes:
            units.append(unit(current))
            current, current_bytes = [], header_bytes
        current.append(hunk)
        current_bytes += hunk_bytes
    if current:
        units.append(unit(current))
    return units


def chunk_diff(parsed: ParsedDiff, max_bytes: int) -> list[ParsedDiff]:
    """把可检视文件打包为若干分片；同一文件的多个单元不会落在同一分片。"""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    chunks: list[ParsedDiff] = []
    current: list[DiffFile] = []
    current_bytes = 0
    current_paths: set[str] = set()
    for file in parsed.reviewable_files:
        for unit in _file_units(file, max_bytes):
            unit_bytes = len(unit.raw.encode())
            if current and (current_bytes + unit_bytes > max_bytes or unit.path in current_paths):
                chunks.append(ParsedDiff(files=current))
                current, current_bytes, current_paths = [], 0, set()
            current.append(unit)
            current_bytes += unit_bytes
            current_paths.add(unit.path)
    if current:
        chunks.append(ParsedDiff(files=current))
    return chunks


def plan_review_chunks(parsed: ParsedDiff, max_bytes: int, *, large_new_file_lines: int) -> list[ReviewChunk]:
    """常规文件按字节预算分片；新增行数严格超过阈值的新文件单独整文件直检。"""
    regular: list[DiffFile] = []
    direct: list[ReviewChunk] = []
    for file in parsed.reviewable_files:
        if file.change_type == "added" and len(file.added_lines) > large_new_file_lines:
            direct.append(ReviewChunk(ParsedDiff(files=[file]), direct_llm=True))
        else:
            regular.append(file)
    return [ReviewChunk(chunk) for chunk in chunk_diff(ParsedDiff(files=regular), max_bytes)] + direct
