"""系统状态：健康检查、统计、分类枚举。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.cache import health as cache_health
from app.cache import is_configured as is_redis_configured
from app.config import (
    CATEGORY_FILTER_OPTIONS,
    DEFAULT_VERSION,
    DOCUMENT_CATEGORIES,
    SUPPORTED_FILE_TYPES,
    settings,
)
from app.db.session import check_connection
from app.schemas import BaseResponse
from app.security import CurrentUser, get_current_user
from app.services import storage
from app.services.embeddings import describe_provider, get_provider, is_degraded, provider_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["系统状态"])


@router.get("/health", summary="健康检查")
def health() -> BaseResponse:
    """数据库 / 嵌入 / 对话模型 / Redis 四项。

    嵌入服务没起来时检索会静默降级成"未找到明确依据"，是检索问题里最常见的原因，
    所以单独探测并把状态摆到界面上。

    Redis 同样是**降级而非失败**：没配或连不上时功能都在（走进程内实现），
    但多实例部署下登录限流与令牌吊销会各自为政，所以要把这件事摆出来。
    """
    database_ok, database_detail = check_connection()

    degraded = is_degraded()
    embedding_ok, embedding_detail = _probe_embedding()

    api_key_configured = bool(settings.llm_api_key)

    redis_ok, redis_detail = cache_health()

    # hash 伪嵌入永远算 degraded：它能让链路跑通，但检索没有语义意义
    all_ok = (
        database_ok and embedding_ok and api_key_configured and not degraded and redis_ok
    )

    return BaseResponse.success(
        data={
            "status": "ok" if all_ok else "degraded",
            "postgres": {"ok": database_ok, "detail": database_detail},
            "embedding": {
                "ok": embedding_ok,
                "provider": provider_name(),
                "detail": embedding_detail,
            },
            "llm": {
                "ok": api_key_configured,
                "detail": "已配置 LLM_API_KEY"
                if api_key_configured
                else "未配置 LLM_API_KEY，问答会失败",
            },
            "redis": {
                "ok": redis_ok,
                "configured": is_redis_configured(),
                "detail": redis_detail,
            },
        }
    )


def _probe_embedding() -> tuple[bool, str]:
    if is_degraded():
        return True, (
            "当前使用 hash 伪嵌入：链路可跑通，但检索结果**没有语义意义**。"
            "正式使用请把 EMBEDDING_PROVIDER 改为 ollama 或 openai。"
        )
    try:
        get_provider().embed_query("健康检查")
        return True, describe_provider()
    except Exception as exc:  # noqa: BLE001 - 健康检查要把异常转成可读描述
        return False, f"{describe_provider()} 不可用（{type(exc).__name__}: {exc}）"


@router.get("/stats", summary="知识库统计")
def stats(user: CurrentUser = Depends(get_current_user)) -> BaseResponse:
    """文档数 / 分类数 / 问答数 / 反馈数。

    管理员额外拿到用户数——统计页要展示"待审批"角标。
    """
    data = storage.get_stats()
    if user.is_admin:
        from app.services import users

        data["user_count"] = len(users.list_users())
        data["pending_user_count"] = len(users.list_users(status="pending"))
    return BaseResponse.success(data=data)


@router.get("/categories", summary="分类与支持的文件类型")
def categories() -> BaseResponse:
    """前端的上传表单与筛选下拉都读这里，避免两处各写一份而漂移。"""
    return BaseResponse.success(
        data={
            "categories": DOCUMENT_CATEGORIES,
            "filter_options": CATEGORY_FILTER_OPTIONS,
            "supported_file_types": SUPPORTED_FILE_TYPES,
            "default_version": DEFAULT_VERSION,
            "upload_max_mb": settings.upload_max_mb,
        }
    )
