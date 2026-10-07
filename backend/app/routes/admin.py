"""管理后台接口（全部要求管理员）。

这是**注册审批的唯一入口**：用户注册后处于 ``pending``，登录会被拒；
必须由管理员在这里把它置为 ``approved`` 才能登录。

管理员 = root 权限：跨用户全量可见可管（文档、会话、问答、反馈）。
唯一不可变的是 root 账号本身——不可删除、不可改角色、不可被注册，
避免一次误操作把系统锁死（再没有管理员能进后台改回来）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.config import settings
from app.schemas import (
    CODE_BAD_REQUEST,
    BaseResponse,
    DeleteUserRequest,
    ReviewUserRequest,
    SetRoleRequest,
)
from app.security import CurrentUser, require_admin
from app.services import storage, users

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin",
    tags=["管理后台"],
    dependencies=[Depends(require_admin)],
)


# --------------------------------------------------------------------------- 用户审批


@router.get("/users", summary="用户列表（可按状态过滤）")
def list_users(status: str | None = None) -> BaseResponse:
    """用户列表。返回里**不含口令哈希**。

    ``status`` 可取 ``pending`` / ``approved`` / ``rejected``；
    不传则返回全部。前端审批页默认用 ``pending``，
    并轮询它来显示侧边栏的待审批角标。
    """
    if status and status not in ("pending", "approved", "rejected"):
        return BaseResponse.error(
            "status 只能是 pending / approved / rejected", code=CODE_BAD_REQUEST
        )
    records = users.list_users(status=status)
    return BaseResponse.success(
        data={
            "users": records,
            "counts": users.count_by_status(),
            "admin_username": settings.admin_username,
            "allow_admin_promotion": settings.allow_admin_promotion,
        }
    )


@router.post("/users/review", summary="审批用户（通过 / 拒绝 / 重置）")
def review_user(payload: ReviewUserRequest) -> BaseResponse:
    """把用户置为 ``approved`` / ``rejected`` / ``pending``。

    被拒绝的账号**不删行**，只置状态：这样对方重新注册时能复用同一行，
    而不是"账号凭空消失、同名再也注册不了"。
    """
    result = users.set_user_status(payload.username, payload.status)
    if result["status"] == "error":
        return BaseResponse.error(result["message"], code=CODE_BAD_REQUEST)
    return BaseResponse.success(msg=result["message"])


@router.post("/users/role", summary="提升 / 降级用户角色")
def set_role(payload: SetRoleRequest) -> BaseResponse:
    """把已通过审批的用户设为 ``admin`` 或 ``user``。

    受 ``ALLOW_ADMIN_PROMOTION`` 约束；**root 自身不可被降级**。
    """
    result = users.set_user_role(payload.username, payload.role)
    if result["status"] == "error":
        return BaseResponse.error(result["message"], code=CODE_BAD_REQUEST)
    return BaseResponse.success(msg=result["message"])


@router.post("/users/delete", summary="删除用户")
def delete_user(payload: DeleteUserRequest) -> BaseResponse:
    """删除注册用户。**root 账号不可删除。**"""
    result = users.delete_user(payload.username)
    if result["status"] == "error":
        return BaseResponse.error(result["message"], code=CODE_BAD_REQUEST)
    return BaseResponse.success(msg=result["message"])


# --------------------------------------------------------------------------- 问答与反馈


@router.get("/qa_logs", summary="全部问答记录")
def qa_logs(limit: int | None = None) -> BaseResponse:
    """全部用户的问答记录。"""
    return BaseResponse.success(data=storage.list_qa_logs(limit=limit))


@router.get("/feedback", summary="全部反馈记录")
def feedback() -> BaseResponse:
    records = storage.list_feedback()
    helpful = sum(1 for r in records if r.get("rating") == "helpful")
    needs_improvement = sum(1 for r in records if r.get("rating") == "needs_improvement")
    return BaseResponse.success(
        data={
            "feedback": records,
            "summary": {
                "total": len(records),
                "helpful": helpful,
                "needs_improvement": needs_improvement,
            },
        }
    )


# --------------------------------------------------------------------------- 可观测性
#
# **这些接口只有管理员能访问**：整个 router 挂着 ``require_admin`` 依赖
# （见文件顶部），所以非管理员会拿到 403、未登录拿到 401。
#
# 前端也隐藏了入口，但**前端隐藏不是安全边界**——真正的拦截在这里。
# 观测数据包含"谁在什么时候调了什么接口"，属于运维信息，不该给普通用户看。


@router.get("/observability/overview", summary="观测总览")
def observability_overview(hours: int = 24) -> BaseResponse:
    """请求量、错误率、耗时百分位、token 合计、写入器状态。

    ``hours`` 是统计窗口（默认 24 小时）。
    """
    from app.observability import get_writer
    from app.services import observability_store

    data = observability_store.overview(hours=hours)
    # 把写入器自身状态一并给出：队列满了会**丢弃**轨迹（这是刻意的设计），
    # 但"丢了"必须可见，否则统计数字会静静地少一块。
    data["writer"] = get_writer().stats()
    data["slowest_routes"] = observability_store.slowest_routes(hours=hours)
    data["notice"] = (
        "只采集结构性信息（路由模板、状态码、耗时、用户名），"
        "不采集请求体、query string、header、口令或 token。"
    )
    return BaseResponse.success(data=data)


@router.get("/observability/requests", summary="请求轨迹")
def observability_requests(
    limit: int = 50,
    offset: int = 0,
    route: str = "",
    status: str = "",
    username: str = "",
) -> BaseResponse:
    """最近的请求轨迹。``status`` 支持 ``4xx`` 这种粗粒度写法或精确数字。"""
    from app.services import observability_store

    return BaseResponse.success(
        data=observability_store.list_requests(
            limit=limit, offset=offset, route=route, status=status, username=username
        )
    )


@router.get("/observability/llm", summary="按 Agent 的模型用量")
def observability_llm(hours: int = 24) -> BaseResponse:
    """哪个 Agent 最贵——按 token 分组统计。

    ``usage_missing`` 的调用会被单独标出：上游没返回用量时**不猜**，
    所以"0 token"与"不知道"是两件事，不能混为一谈。
    """
    from app.services import observability_store

    return BaseResponse.success(data=observability_store.llm_breakdown(hours=hours))


@router.get("/observability/cache", summary="缓存状态")
def observability_cache() -> BaseResponse:
    """Redis 缓存命中相关统计（复用 app.cache.stats()）。

    没配 Redis 时返回进程内实现的规模——那是**正常状态**，不是故障。
    """
    from app.cache import health as cache_health
    from app.cache import is_configured, stats

    ok, detail = cache_health()
    return BaseResponse.success(
        data={
            "ok": ok,
            "configured": is_configured(),
            "detail": detail,
            "stats": stats(),
        }
    )
