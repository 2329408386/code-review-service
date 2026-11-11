"""解析 Codex 的输出：最终 JSON 结论、JSONL 事件流中的进度与 Token 用量。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..models import ReviewOutput

# 只做位置字段的别名兼容；其余字段仍由 Pydantic 严格校验
_PATH_ALIASES = ("file", "path", "newPath")
_LINE_ALIASES = ("line", "newLine")
_WRAPPER_KEYS = ("result", "output", "response")

# Codex JSONL 中已完成事项的中文标签
_ITEM_LABELS = {
    "reasoning": "推理摘要",
    "agent_message": "推理步骤",
    "command_execution": "命令执行",
    "function_call": "工具调用",
    "function_call_output": "工具结果",
    "tool_call": "工具调用",
    "tool_call_output": "工具结果",
    "file_read": "文件读取",
    "file_change": "文件修改",
    "web_search": "网络搜索",
    "mcp_tool_call": "MCP 调用",
    "error": "提示",
}


@dataclass(frozen=True)
class Progress:
    message: str
    detail: str


def repair_json_text(value: str) -> str:
    """只修复机械性、无歧义的 JSON 缺陷：字符串内的原始换行/控制字符，以及尾随逗号。"""
    result: list[str] = []
    in_string = escaped = False
    for index, char in enumerate(value):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            elif ord(char) < 0x20:
                result.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(char, f"\\u{ord(char):04x}"))
                continue
        elif char == '"':
            in_string = True
        elif char == ",":
            rest = value[index + 1 :].lstrip()
            if rest[:1] in ("}", "]"):
                continue
        result.append(char)
    return "".join(result)


def _normalize_review_object(parsed: object) -> object:
    """兼容网关的常见变形：外层包装、顶层直接是数组、字段别名、大小写与超长文本。"""
    if isinstance(parsed, list):
        parsed = {"summary": "", "findings": parsed}
    if not isinstance(parsed, dict):
        return parsed
    for key in _WRAPPER_KEYS:
        if isinstance(parsed.get(key), dict) and "findings" in parsed[key]:
            parsed = parsed[key]
            break
    if not isinstance(parsed.get("findings"), list):
        return parsed
    parsed = dict(parsed)
    if isinstance(parsed.get("summary"), str):
        parsed["summary"] = parsed["summary"].strip()[:4000]
    findings = []
    for finding in parsed["findings"]:
        if isinstance(finding, dict):
            finding = dict(finding)
            for alias in _PATH_ALIASES:
                if alias in finding:
                    finding.setdefault("new_path", finding.pop(alias))
            for alias in _LINE_ALIASES:
                if alias in finding:
                    finding.setdefault("new_line", finding.pop(alias))
            if "oldPath" in finding:
                finding["old_path"] = finding.pop("oldPath")
            for key, limit in (("severity", None), ("title", 300), ("body", 8000)):
                if isinstance(finding.get(key), str):
                    text = finding[key].strip()
                    finding[key] = text.lower() if limit is None else text[:limit]
        findings.append(finding)
    parsed["findings"] = findings
    return parsed


def _json_candidates(raw: str) -> list[object]:
    """从模型输出中找出所有可能的 JSON 结论：代码围栏优先，其次整体文本，最后扫描正文中的对象。"""
    candidates: list[object] = []

    def try_load(text: str) -> None:
        text = text.strip().lstrip("﻿")
        for variant in dict.fromkeys((text, repair_json_text(text))):
            try:
                value = json.loads(variant)
            except json.JSONDecodeError:
                continue
            # 有的网关把 JSON 对象再序列化成字符串
            for _ in range(2):
                if not isinstance(value, str):
                    break
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    break
            candidates.append(value)

    fences = re.findall(r"```\s*([A-Za-z0-9_-]*)\s*\n(.*?)```", raw, flags=re.DOTALL)
    for _, body in sorted(fences, key=lambda fence: fence[0].lower() != "json"):
        try_load(body)
    try_load(raw)
    decoder = json.JSONDecoder()
    for source in dict.fromkeys((raw, repair_json_text(raw))):
        for match in re.finditer(r"\{", source):
            try:
                candidates.append(decoder.raw_decode(source, match.start())[0])
            except json.JSONDecodeError:
                continue
    return candidates


def parse_review_output(raw: str) -> ReviewOutput:
    """返回第一个能通过校验的结论；全部失败时抛出最后一个校验错误。"""
    candidates = _json_candidates(raw)
    if not candidates:
        raise json.JSONDecodeError("no JSON object found in model output", raw, 0)
    error: ValueError | None = None
    for candidate in candidates:
        try:
            return ReviewOutput.model_validate(_normalize_review_object(candidate))
        except ValueError as exc:
            error = exc
    raise error  # type: ignore[misc]


def token_usage(event: object) -> tuple[int, int, int]:
    """累加一行 JSONL 事件中所有名为 usage 的对象：(输入, 输出, 缓存输入)。"""
    totals = [0, 0, 0]

    def is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def walk(value: object) -> None:
        if isinstance(value, dict):
            usage = value.get("usage")
            if isinstance(usage, dict) and is_int(usage.get("input_tokens")) and is_int(usage.get("output_tokens")):
                totals[0] += max(0, usage["input_tokens"])
                totals[1] += max(0, usage["output_tokens"])
                if is_int(usage.get("cached_input_tokens")):
                    totals[2] += max(0, usage["cached_input_tokens"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(event)
    return totals[0], totals[1], totals[2]


def _item_text(item: dict) -> str:
    """把一个已完成事项中可读的文本拼接起来（消息、推理、命令、工具参数与结果等）。"""
    parts: list[str] = []

    def push(value: object) -> None:
        if isinstance(value, list):
            for part in value:
                if isinstance(part, dict):
                    push(part.get("title"))
                    push(part.get("text") or part.get("summary"))
                else:
                    push(part)
        elif value not in (None, ""):
            parts.append(str(value))

    for key in ("text", "summary", "message"):
        push(item.get(key))
    for block in item.get("content") or []:
        if isinstance(block, dict):
            push(block.get("text"))
            push(block.get("name"))
            push(block.get("arguments"))
            push(block.get("output"))
    for key in ("name", "arguments", "output", "command", "aggregated_output"):
        push(item.get(key))
    return "\n".join(parts).replace("\r\n", "\n").strip()


def progress_from_event(event: object) -> Progress | None:
    """把 item.completed 事件转为时间线进度；最终的 JSON 结论单独展示，不进时间线。"""
    if not isinstance(event, dict) or event.get("type") != "item.completed":
        return None
    item = event.get("item")
    if not isinstance(item, dict):
        return None
    detail = _item_text(item)[:4000]
    item_type = str(item.get("type", ""))
    if not detail or (item_type == "agent_message" and detail.lstrip().startswith(("{", "```json"))):
        return None
    return Progress(_ITEM_LABELS.get(item_type, "事件"), detail)


def agent_message(event: object) -> str | None:
    """提取 agent_message 文本，用作 --output-last-message 缺失时的结论备选来源。"""
    if not isinstance(event, dict) or event.get("type") != "item.completed":
        return None
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "agent_message":
        return None
    text = item.get("text")
    return str(text).strip() or None if text else None


def stream_error(event: object) -> str | None:
    """提取 turn.failed / error 事件中的错误信息（重连提示不算最终错误）。"""
    if not isinstance(event, dict):
        return None
    if event.get("type") == "turn.failed" and isinstance(event.get("error"), dict):
        return str(event["error"].get("message") or "") or None
    if event.get("type") == "error":
        message = str(event.get("message") or "")
        return None if message.startswith("Reconnecting") else message or None
    return None
