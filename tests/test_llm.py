"""大模型层：模型路由、Codex CLI 调用（伪造 CLI）、OpenAI 兼容 Chat 调用。"""
from __future__ import annotations

import httpx
import pytest
from helpers import codex_calls, fake_codex

from app.llm import chat
from app.llm.codex import CodexClient, CodexError, safe_environment
from app.llm.registry import Gateway, ModelConfigurationError, ModelRegistry

GOOD = {"summary": "发现问题", "findings": [{"new_path": "app.py", "new_line": 2, "severity": "major", "title": "t", "body": "b"}]}


# ---------------------------------------------------------------- 模型路由


def test_registry_seeds_from_env_model(settings, database):
    settings.openai_model = "gemini"
    registry = ModelRegistry(database, settings)
    route = registry.route_for("any-repo")
    assert route.name == "gemini" and registry.model_names() == ["gemini"]
    assert registry.configuration()["default_model"] == route.id


def test_registry_routes_keys_and_validation(settings, database):
    settings.openai_api_base_url = "http://env/v1"
    settings.openai_api_key = "env-key"
    registry = ModelRegistry(database, settings)
    assert registry.route_for("demo") is None  # 未配置模型

    config = registry.replace(
        [
            {"name": "m1", "concurrency": 2},
            {"name": "m2", "repositories": ["demo"], "base_url": "http://gw/v1/", "api_key": "k2", "reasoning_effort": "high"},
        ],
        "m1",
    )
    m1, m2 = config["models"]
    assert config["default_model"] == m1["id"]
    assert "api_key" not in m2 and m2["api_key_configured"] and m2["base_url"] == "http://gw/v1"
    assert registry.route_for("demo").name == "m2"
    assert registry.route_for("other").name == "m1"
    assert registry.gateway_for(registry.get(m2["id"])) == Gateway("http://gw/v1", "k2")
    assert registry.gateway_for(registry.get(m1["id"])) == Gateway("http://env/v1", "env-key")

    # API Key 留空时保留已保存的值；配置持久化
    registry.replace([{**m2, "api_key": ""}], None)
    reloaded = ModelRegistry(database, settings)
    assert reloaded.gateway_for(reloaded.route_for("demo")).api_key == "k2"

    for bad in (
        [],
        [{"name": "bad name!"}],
        [{"name": "a", "repositories": ["x"]}, {"name": "b", "repositories": ["x"]}],
        [{"name": "a", "base_url": "ftp://x"}],
        [{"name": "a", "base_url": "http://x"}],  # 有 Base URL 无 Key
        [{"name": "a", "reasoning_effort": "extreme"}],
    ):
        with pytest.raises(ModelConfigurationError):
            registry.replace(bad, None)


# ---------------------------------------------------------------- Codex CLI


def _client(settings, database, tmp_path, monkeypatch, outputs, **kwargs):
    bin_dir = tmp_path / "bin"
    fake_codex(bin_dir, outputs, monkeypatch=monkeypatch, **kwargs)
    settings.openai_api_base_url = "http://gw/v1"
    settings.openai_api_key = "secret-key"
    settings.openai_model = "gemini"
    registry = ModelRegistry(database, settings)
    return CodexClient(settings, registry), registry, bin_dir


def _review(client, registry, tmp_path, progress=None):
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    return client.review(
        worktree=worktree, prompt="PROMPT", task_id="t1", label="discovery-1",
        route=registry.route_for("demo"), log_identity=("demo", "feature/x"), on_progress=progress,
    )


def test_codex_review_success(settings, database, tmp_path, monkeypatch):
    client, registry, bin_dir = _client(settings, database, tmp_path, monkeypatch, [GOOD])
    assert client.available and client.version == "codex-cli 9.9.9"
    seen = []
    result = _review(client, registry, tmp_path, seen.append)
    assert result.output.findings[0].title == "t"
    assert (result.input_tokens, result.output_tokens, result.cached_input_tokens) == (100, 20, 5)
    assert result.model == "gemini"
    assert seen and seen[0].detail == "正在阅读 app.py"

    [call] = codex_calls(bin_dir)
    argv = call["argv"]
    assert call["prompt"] == "PROMPT" and call["key"] == "secret-key"
    assert "model_provider=code-review-gateway" in argv
    assert "model_providers.code-review-gateway.base_url=http://gw/v1" in argv
    assert argv[argv.index("--model") + 1] == "gemini"
    assert "--skip-git-repo-check" in argv
    assert (settings.codex_log_dir / "t1.discovery-1.attempt-1.json").exists()
    assert list(settings.prompt_log_dir.iterdir())


def test_codex_retries_invalid_output_then_succeeds(settings, database, tmp_path, monkeypatch):
    client, registry, bin_dir = _client(settings, database, tmp_path, monkeypatch, ["not json", GOOD])
    result = _review(client, registry, tmp_path)
    assert len(result.output.findings) == 1
    calls = codex_calls(bin_dir)
    assert len(calls) == 2 and calls[1]["prompt"] != "PROMPT"  # 重试时追加格式提示
    assert result.input_tokens == 200  # 失败尝试的 Token 也计入


def test_codex_failure_raises(settings, database, tmp_path, monkeypatch):
    client, registry, _ = _client(settings, database, tmp_path, monkeypatch, [GOOD], exit_code=3)
    with pytest.raises(CodexError, match="exited with code 3"):
        _review(client, registry, tmp_path)


def test_codex_without_gateway_uses_local_config(settings, database, tmp_path, monkeypatch):
    fake_codex(tmp_path / "bin", [GOOD], monkeypatch=monkeypatch)
    client = CodexClient(settings, ModelRegistry(database, settings))
    worktree = tmp_path / "wt"
    worktree.mkdir()
    client.review(worktree=worktree, prompt="P", task_id="t", label="x", route=None, log_identity=("r", "b"))
    argv = codex_calls(tmp_path / "bin")[0]["argv"]
    assert not any("model_provider" in arg for arg in argv) and "--model" not in argv


def test_safe_environment_filters_secrets(monkeypatch):
    monkeypatch.setenv("CODEHUB_PRIVATE_TOKEN", "x")
    monkeypatch.setenv("OPENAI_API_KEY", "y")
    monkeypatch.setenv("HOME_HINT", "keep")
    env = safe_environment("gw")
    assert "CODEHUB_PRIVATE_TOKEN" not in env and "OPENAI_API_KEY" not in env
    assert env["HOME_HINT"] == "keep" and env["CODE_REVIEW_LLM_API_KEY"] == "gw"


# ---------------------------------------------------------------- Chat


def test_chat_list_models_and_complete(monkeypatch):
    gateway = Gateway("http://gw/v1", "k")

    def fake_get(url, headers, timeout):
        assert url == "http://gw/v1/models" and headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "a"}, {"id": "b"}]}, request=httpx.Request("GET", url))

    attempts = []

    def fake_post(url, headers, json, timeout):
        attempts.append(json)
        if len(attempts) == 1:
            return httpx.Response(503, request=httpx.Request("POST", url))
        body = {"choices": [{"message": {"content": " 好 "}}], "usage": {"prompt_tokens": 9, "completion_tokens": 4}}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    monkeypatch.setattr(chat.httpx, "get", fake_get)
    monkeypatch.setattr(chat.httpx, "post", fake_post)
    assert chat.list_models(gateway) == ["a", "b"]
    result = chat.complete(gateway, "m", "prompt", timeout=5, max_attempts=2)
    assert (result.text, result.input_tokens, result.output_tokens) == ("好", 9, 4)
    assert attempts[0]["model"] == "m"


def test_chat_errors(monkeypatch):
    gateway = Gateway("http://gw/v1", "k")
    monkeypatch.setattr(
        chat.httpx, "get", lambda url, **_: httpx.Response(401, request=httpx.Request("GET", url))
    )
    with pytest.raises(chat.ChatError, match="401"):
        chat.list_models(gateway)
    monkeypatch.setattr(
        chat.httpx, "post", lambda url, **_: httpx.Response(500, request=httpx.Request("POST", url))
    )
    with pytest.raises(chat.ChatError, match="after 2 attempts"):
        chat.complete(gateway, "m", "p", timeout=1, max_attempts=2)


def test_registry_candidates_cooldown_and_recovery(settings, database, monkeypatch):
    from app.llm import registry as registry_module

    now = [1000.0]
    monkeypatch.setattr(registry_module, "_clock", lambda: now[0])
    registry = ModelRegistry(database, settings)
    registry.replace([{"name": "a", "repositories": ["demo"]}, {"name": "b"}, {"name": "c"}], "b")
    names = lambda: [route.name for route in registry.candidates("demo")]  # noqa: E731
    assert names() == ["a", "b", "c"]
    a = registry.route_for("demo")
    assert registry.report_failure(a, "boom") is False  # 未达到阈值，只切换当次调用
    assert names() == ["a", "b", "c"]
    registry.report_success(a)
    registry.report_failure(a, "boom")
    assert registry.report_failure(a, "boom") is True  # 连续失败达到阈值
    assert names() == ["b", "c", "a"]
    assert registry.configuration()["health"][a.id]["available"] is False
    now[0] += registry_module.COOLDOWN_SECONDS + 1
    assert names() == ["a", "b", "c"]  # 冷却结束后重新优先尝试
    registry.report_success(a)
    assert registry.configuration()["health"] == {}
