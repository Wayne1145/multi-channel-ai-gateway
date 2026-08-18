"""平台模型组解析与串行故障切换。"""

import logging
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ModelGroup, ModelRoute, PlatformProvider, UserSettings
from .providers import CompletionResult, RetryableProviderError, provider_for
from .redaction import redact_error
from .security import decrypt_secret

log = logging.getLogger(__name__)


@dataclass
class RoutedCompletion:
    content: str
    provider_name: str
    provider_key: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    group_id: str | None = None
    route_id: str | None = None


def _is_failover_error(exc: Exception) -> bool:
    """仅对临时上游故障切换；鉴权和请求参数错误必须直接暴露给管理员。"""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 409, 425, 429} or exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, RetryableProviderError))


def active_routes(
    db: Session, group_id: str | None = None
) -> list[tuple[ModelGroup, ModelRoute, PlatformProvider]]:
    query = select(ModelGroup).where(ModelGroup.enabled.is_(True))
    if group_id is not None:
        query = query.where(ModelGroup.id == group_id)
    else:
        query = query.where(ModelGroup.is_default.is_(True))
    group = db.scalar(query.order_by(ModelGroup.created_at, ModelGroup.id))
    if group is None:
        return []
    return list(
        db.execute(
            select(ModelGroup, ModelRoute, PlatformProvider)
            .join(ModelRoute, ModelRoute.group_id == ModelGroup.id)
            .join(PlatformProvider, PlatformProvider.id == ModelRoute.provider_id)
            .where(
                ModelGroup.id == group.id,
                ModelRoute.enabled.is_(True),
                PlatformProvider.enabled.is_(True),
            )
            .order_by(ModelRoute.priority, ModelRoute.created_at, ModelRoute.id)
        ).all()
    )


async def complete_with_routing(
    db: Session,
    user_settings: UserSettings,
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int,
    timeout: float,
    group_id: str | None = None,
) -> RoutedCompletion:
    """使用默认模型组完成请求；可重试错误按优先级切到下一条路由。"""
    routes = active_routes(db, group_id)
    if not routes:
        raise RuntimeError("未配置可用的平台模型组")

    last_error: Exception | None = None
    for group, route, platform_provider in routes:
        try:
            api_key = decrypt_secret(platform_provider.api_key_encrypted)
            result: CompletionResult = await provider_for(
                platform_provider.provider_key,
                platform_provider.base_url,
                api_key,
                timeout,
            ).complete(messages, route.model, temperature, max_tokens)
            return RoutedCompletion(
                content=result.content,
                provider_name=platform_provider.name,
                provider_key=platform_provider.provider_key,
                model=route.model,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                group_id=group.id,
                route_id=route.id,
            )
        except Exception as exc:
            if not _is_failover_error(exc):
                raise
            last_error = exc
            log.warning(
                "模型路由失败，准备切换 group=%s route=%s provider=%s error=%s",
                group.id,
                route.id,
                platform_provider.id,
                redact_error(exc, 300),
            )
    assert last_error is not None
    raise last_error
