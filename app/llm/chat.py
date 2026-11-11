"""OpenAI 兼容接口的轻量调用：拉取模型列表、单轮文本补全（用于 MR 评价摘要）。

纯文本任务不需要 Codex 的仓库代理能力，直接调用 /chat/completions 更快、更省 Token。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .registry import Gateway

LOGGER = logging.getLogger(__name__)


class ChatError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0


def _headers(api_key: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def list_models(gateway: Gateway, *, timeout: float = 30.0) -> list[str]:
    """GET {base_url}/models，返回去重后的模型 ID 列表。"""
    try:
        response = httpx.get(f"{gateway.base_url}/models", headers=_headers(gateway.api_key), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise ChatError(f"网关返回 HTTP {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise ChatError(str(exc)) from exc
    items = payload.get("data") if isinstance(payload, dict) else payload
    names = [
        item if isinstance(item, str) else item.get("id")
        for item in items or []
        if isinstance(item, str) or (isinstance(item, dict) and isinstance(item.get("id"), str))
    ]
    return list(dict.fromkeys(name for name in names if name))


def complete(gateway: Gateway, model: str, prompt: str, *, timeout: float, max_attempts: int) -> ChatResult:
    """POST {base_url}/chat/completions；网络错误和 5xx/429 会重试。"""
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2}
    error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = httpx.post(
                f"{gateway.base_url}/chat/completions",
                headers=_headers(gateway.api_key),
                json=body,
                timeout=timeout,
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise ChatError(f"HTTP {response.status_code}")
            response.raise_for_status()
            payload = response.json()
            text = payload["choices"][0]["message"]["content"] or ""
            usage = payload.get("usage") or {}
            return ChatResult(
                text=str(text).strip(),
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            )
        except (ChatError, httpx.TransportError) as exc:
            error = exc
            LOGGER.warning("chat completion attempt %s/%s failed: %s", attempt, max_attempts, exc)
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise ChatError(f"chat completion failed: {exc}") from exc
    raise ChatError(f"chat completion failed after {max_attempts} attempts: {error}")
