"""运行时配置：模型路由、检视范围、代码仓管理（读取公开，修改需管理员）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..llm import chat
from ..llm.registry import REASONING_EFFORTS, Gateway, ModelConfigurationError, validate_base_url
from ..models import ModelConfigurationInput, ModelDiscoveryRequest, RepositoryCatalogInput, ReviewScopeInput
from ..review.repos import RepositoryCatalogError
from ..review.scope import ReviewScopeError
from .deps import AdminOnly, Runtime

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("/models")
def get_models(runtime: Runtime) -> dict:
    return runtime.registry.configuration()


@router.put("/models", dependencies=[AdminOnly])
def update_models(configuration: ModelConfigurationInput, runtime: Runtime) -> dict:
    try:
        return runtime.registry.replace(
            [model.model_dump() for model in configuration.models], configuration.default_model
        )
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/llm/models", dependencies=[AdminOnly])
def discover_models(request: ModelDiscoveryRequest, runtime: Runtime) -> dict:
    """拉取网关可用模型：未填写的 Base URL / Key 依次回退到该路由已保存的值、.env 默认网关。"""
    fallback = runtime.registry.gateway_for(runtime.registry.get(request.route_id))
    try:
        gateway = Gateway(
            validate_base_url(request.base_url or fallback.base_url), request.api_key or fallback.api_key
        )
        models = chat.list_models(gateway)
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except chat.ChatError as exc:
        raise HTTPException(status_code=502, detail=f"拉取模型失败：{exc}") from exc
    if not models:
        raise HTTPException(status_code=502, detail="拉取模型失败：网关未返回模型")
    return {"models": models, "reasoning_efforts": list(REASONING_EFFORTS)}


@router.get("/review-scope")
def get_review_scope(runtime: Runtime) -> dict:
    return {"rules": runtime.scope.rules()}


@router.put("/review-scope", dependencies=[AdminOnly])
def update_review_scope(configuration: ReviewScopeInput, runtime: Runtime) -> dict:
    try:
        return {"rules": runtime.scope.replace([rule.model_dump() for rule in configuration.rules])}
    except ReviewScopeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _repository_catalog(runtime: Runtime) -> dict:
    """所有出现过的代码仓（含已隐藏）及其检视次数、最近检视时间、分组与隐藏标记。"""
    configuration = runtime.catalog.configuration()
    hidden = set(configuration["hidden"])
    owner = {repo: group["name"] for group in configuration["groups"] for repo in group["repositories"]}
    repositories = [
        row | {"hidden": row["name"] in hidden, "group": owner.get(row["name"])}
        for row in runtime.database.repositories()
    ]
    return configuration | {"repositories": repositories}


@router.get("/repositories")
def get_repositories(runtime: Runtime) -> dict:
    return _repository_catalog(runtime)


@router.put("/repositories", dependencies=[AdminOnly])
def update_repositories(configuration: RepositoryCatalogInput, runtime: Runtime) -> dict:
    try:
        runtime.catalog.replace(configuration.model_dump())
    except RepositoryCatalogError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _repository_catalog(runtime)
