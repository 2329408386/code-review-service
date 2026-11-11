"""模型路由：按代码仓选择模型、网关和并发上限，配置持久化在 runtime_settings 中。

- 每条路由可单独配置 Base URL / API Key；未配置时使用 .env 中的 OPENAI_API_BASE_URL / OPENAI_API_KEY。
- 代码仓绑定到某条路由；未绑定的仓库使用“兜底”路由。
- 每条路由有独立的并发信号量，保存配置后立即生效。
- 故障切换：路由连续调用失败达到阈值后进入冷却期，期间该路由的代码仓临时改用其它健康路由；
  冷却期结束后重新优先尝试原路由，调用成功即恢复（自动切回）。健康状态只保存在内存中。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

from ..config import Settings
from ..storage import Database

LOGGER = logging.getLogger(__name__)
SETTING_KEY = "codex_models"
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_NAME_CHARS = "._:/-"
# 未指定并发度时，每条路由同时运行的 Codex 进程数
DEFAULT_CONCURRENCY = 5
# 连续失败多少次后判定路由异常（单次失败只切换当次调用，不影响其它任务）
FAILURE_THRESHOLD = 2
# 判定异常后的冷却时间，到期后重新尝试原路由
COOLDOWN_SECONDS = 300.0
# 冷却计时使用的时钟（测试中替换）
_clock = time.monotonic


class ModelConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Gateway:
    """OpenAI 兼容网关。两项都为空表示使用 Codex 本机默认配置。"""

    base_url: str = ""
    api_key: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)


@dataclass(frozen=True)
class ModelRoute:
    id: str
    name: str
    concurrency: int
    repositories: tuple[str, ...] = ()
    base_url: str = ""
    api_key: str = ""
    reasoning_effort: str = "medium"

    def public(self) -> dict[str, object]:
        """对外展示时不返回 API Key，只告知是否已配置。"""
        data = asdict(self)
        data["repositories"] = list(self.repositories)
        data["api_key_configured"] = bool(data.pop("api_key"))
        return data


def validate_base_url(value: str) -> str:
    text = (value or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelConfigurationError("Base URL 必须是 http 或 https 地址")
    return text.rstrip("/")


def _validate_name(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text or len(text) > 200 or not text[0].isalnum() or any(
        not (ch.isalnum() or ch in _NAME_CHARS) for ch in text
    ):
        raise ModelConfigurationError(f"非法的{label}：{value!r}")
    return text


@dataclass
class RouteHealth:
    failures: int = 0
    down_until: float = 0.0  # 冷却截止时刻（_clock）；0 表示未判定异常
    down_since: float = 0.0  # time.time()，用于展示
    last_error: str = ""


class ModelRegistry:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self._lock = threading.RLock()
        self._routes: tuple[ModelRoute, ...] = ()
        self._default_id: str | None = None
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._health: dict[str, RouteHealth] = {}
        self._load()

    # ---------------------------------------------------------------- 加载与保存

    def _load(self) -> None:
        stored = self.database.get_setting(SETTING_KEY)
        if stored:
            try:
                data = json.loads(stored)
                self._apply(*self._normalize(data.get("models") or [], data.get("default_model"), {}))
                return
            except (json.JSONDecodeError, AttributeError, ModelConfigurationError) as exc:
                LOGGER.warning("stored model routes are invalid and were ignored: %s", exc)
        # 首次启动：用 .env 的 OPENAI_MODEL 生成一条兜底路由（网关沿用 .env 默认网关）
        if self.settings.openai_model:
            seed = [{"name": self.settings.openai_model, "concurrency": DEFAULT_CONCURRENCY}]
            self._apply(*self._normalize(seed, None, {}))
            self._save()

    def _apply(self, routes: tuple[ModelRoute, ...], default_id: str | None) -> None:
        self._routes = routes
        self._default_id = default_id
        self._semaphores = {route.id: threading.BoundedSemaphore(route.concurrency) for route in routes}
        # 保留仍存在的路由的健康状态，删除的路由一并清理
        self._health = {key: value for key, value in self._health.items() if key in self._semaphores}

    def _save(self) -> None:
        payload = {"default_model": self._default_id, "models": [asdict(route) for route in self._routes]}
        self.database.set_setting(SETTING_KEY, json.dumps(payload, ensure_ascii=False))

    def _normalize(
        self, models: list[dict], default_model: str | None, previous: dict[str, ModelRoute]
    ) -> tuple[tuple[ModelRoute, ...], str]:
        if not models:
            raise ModelConfigurationError("至少需要配置一个模型")
        routes: list[ModelRoute] = []
        bound_repositories: set[str] = set()
        for item in models:
            name = _validate_name(item.get("name", ""), "模型名称")
            route_id = _validate_name(item.get("id") or uuid.uuid4().hex, "路由 ID")
            if any(route.id == route_id for route in routes):
                raise ModelConfigurationError(f"路由 ID 重复：{route_id}")
            old = previous.get(route_id)
            repositories = tuple(dict.fromkeys(str(repo).strip() for repo in item.get("repositories") or []))
            if "" in repositories:
                raise ModelConfigurationError("代码仓名称不能为空")
            if duplicated := bound_repositories.intersection(repositories):
                raise ModelConfigurationError(f"代码仓重复绑定：{', '.join(sorted(duplicated))}")
            bound_repositories.update(repositories)
            effort = item.get("reasoning_effort") or (old.reasoning_effort if old else "medium")
            if effort not in REASONING_EFFORTS:
                raise ModelConfigurationError(f"思考强度必须是 {', '.join(REASONING_EFFORTS)} 之一")
            concurrency = item.get("concurrency") or DEFAULT_CONCURRENCY
            if isinstance(concurrency, bool) or not isinstance(concurrency, int) or not 1 <= concurrency <= 100:
                raise ModelConfigurationError("并发度必须是 1-100 的整数")
            base_url = (item.get("base_url") or "").strip()
            # API Key 留空表示沿用已保存的值
            api_key = item.get("api_key") or (old.api_key if old else "")
            if base_url:
                base_url = validate_base_url(base_url)
                if not api_key:
                    raise ModelConfigurationError(f"模型 {name} 配置了 Base URL，但缺少 API Key")
            routes.append(ModelRoute(route_id, name, concurrency, repositories, base_url, api_key, effort))

        ids = {route.id for route in routes}
        if default_model is None:
            default_model = routes[0].id
        elif default_model not in ids:
            # 兼容按模型名指定兜底（名称唯一时）
            matches = [route.id for route in routes if route.name == default_model]
            if len(matches) != 1:
                raise ModelConfigurationError("兜底模型必须是已配置的模型之一")
            default_model = matches[0]
        return tuple(routes), default_model

    def replace(self, models: list[dict], default_model: str | None) -> dict[str, object]:
        with self._lock:
            routes, default_id = self._normalize(models, default_model, {r.id: r for r in self._routes})
            self._apply(routes, default_id)
            self._save()
        LOGGER.info("model routes updated models=%s default=%s", [r.name for r in routes], default_id)
        return self.configuration()

    # ---------------------------------------------------------------- 查询

    def configuration(self) -> dict[str, object]:
        with self._lock:
            return {
                "default_model": self._default_id,
                "models": [route.public() for route in self._routes],
                "reasoning_efforts": list(REASONING_EFFORTS),
                "env_gateway_configured": self.env_gateway().configured,
                "health": self._health_snapshot(),
            }

    def get(self, route_id: str | None) -> ModelRoute | None:
        with self._lock:
            return next((route for route in self._routes if route.id == route_id), None)

    def route_for(self, repository: str) -> ModelRoute | None:
        """仓库绑定的路由；未绑定时返回兜底路由；完全没有配置时返回 None。"""
        with self._lock:
            bound = next((route for route in self._routes if repository in route.repositories), None)
            return bound or self.get(self._default_id)

    def candidates(self, repository: str) -> list[ModelRoute]:
        """按优先级返回可用于该仓库的路由：首选路由 → 兜底路由 → 其它路由，健康的排在异常的前面。

        首选路由冷却期结束后自动回到首位（下一次调用即“切回”）；全部异常时仍按原顺序尝试。
        没有任何路由时返回空列表（使用 Codex 本机默认模型）。
        """
        with self._lock:
            preferred = self.route_for(repository)
            ordered = list(dict.fromkeys(
                route for route in (preferred, self.get(self._default_id), *self._routes) if route
            ))
            now = _clock()
            return sorted(ordered, key=lambda route: self._is_down(route.id, now))  # 稳定排序

    def _is_down(self, route_id: str, now: float) -> bool:
        health = self._health.get(route_id)
        return bool(health and health.down_until > now)

    def report_failure(self, route: ModelRoute | None, error: str) -> bool:
        """记录一次调用失败；连续失败达到阈值时进入冷却期。返回该路由当前是否被判定异常。"""
        if route is None:
            return False
        with self._lock:
            if route.id not in self._semaphores:
                return False
            health = self._health.setdefault(route.id, RouteHealth())
            health.failures += 1
            health.last_error = error[:500]
            if health.failures >= FAILURE_THRESHOLD:
                if health.down_until <= _clock():
                    LOGGER.warning("model route %s marked unavailable for %.0fs: %s", route.name, COOLDOWN_SECONDS, error)
                    health.down_since = time.time()
                health.down_until = _clock() + COOLDOWN_SECONDS
                return True
            return False

    def report_success(self, route: ModelRoute | None) -> None:
        """调用成功：清除失败计数，异常路由由此恢复。"""
        if route is None:
            return
        with self._lock:
            health = self._health.pop(route.id, None)
        if health and health.down_until:
            LOGGER.info("model route %s recovered", route.name)

    def _health_snapshot(self) -> dict[str, dict[str, object]]:
        """供控制台展示：判定异常的路由及原因、预计重试时间。"""
        now = _clock()
        return {
            route_id: {
                "available": health.down_until <= now,
                "failures": health.failures,
                "last_error": health.last_error,
                "down_since": health.down_since or None,
                "retry_in_seconds": max(0, round(health.down_until - now)),
            }
            for route_id, health in self._health.items()
        }

    def slot(self, route: ModelRoute | None) -> threading.BoundedSemaphore | None:
        with self._lock:
            return self._semaphores.get(route.id) if route else None

    def env_gateway(self) -> Gateway:
        return Gateway(self.settings.openai_api_base_url.strip().rstrip("/"), self.settings.openai_api_key)

    def gateway_for(self, route: ModelRoute | None) -> Gateway:
        """路由自身配置了网关则用自身的，否则用 .env 中的默认网关。"""
        if route and route.base_url:
            return Gateway(route.base_url, route.api_key)
        return self.env_gateway()

    def model_names(self) -> list[str]:
        with self._lock:
            return [route.name for route in self._routes]
