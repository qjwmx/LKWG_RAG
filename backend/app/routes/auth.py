"""注册、登录、当前用户。

鉴权说明
--------
- ``/register`` 与 ``/login`` **刻意不挂任何鉴权**：它们面向的都是尚未登录的用户，
  要求凭据等于没人能注册、没人能登录。
- 登录成功后签发 JWT，客户端后续请求带 ``Authorization: Bearer <token>``，
  身份一律由服务端从 token 解出。
- 管理员专属的接口在 ``admin.py``，统一挂 ``require_admin``。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from app.cache import (
    clear_login_failures,
    login_locked_for,
    register_login_failure,
    revoke_token,
)
from app.config import settings
from app.schemas import (
    CODE_UNAUTHORIZED,
    BaseResponse,
    LoginRequest,
    RegisterRequest,
)
from app.security import CurrentUser, create_access_token, get_current_user
from app.services import users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["用户"])


def _client_ip(request: Request) -> str:
    """取来源 IP。

    **优先读 ``X-Forwarded-For`` 的最左值**：反向代理后 ``request.client``
    永远是代理的地址，按它限流等于把所有人算作同一个人（一个人被锁
    全员被锁）。最左值是原始客户端。

    ⚠️ 这个头是客户端可伪造的，所以它**只用于限流**，不用于任何安全判定。
    限流被绕过最多是"少挡一些爆破"，而身份判定被伪造是越权。
    """
    forwarded = request.headers.get("X-Forwarded-For") or ""
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _throttle_key(username: str, request: Request) -> str:
    """限流标识 = 用户名 + 来源 IP。

    两个都要：只按用户名，攻击者可以换用户名继续试（对同一批账号
    轮着试）；只按 IP，则共享出口（NAT/公司网关）后一个人被锁
    会连累所有人。组合起来既能挡住针对单一账号的爆破，
    也不会因为一个 IP 的失败次数误伤其他账号。
    """
    return f"{(username or '').strip().lower()}|{_client_ip(request)}"


@router.post("/register", summary="提交注册申请")
def register(payload: RegisterRequest) -> BaseResponse:
    """注册一个待审批账号。

    成功返回 ``code=200``；用户名/口令不合规或已被占用时返回非 200 的 code，
    并给出可直接展示给用户的中文说明。

    **注册后不能登录**，必须由管理员在管理后台「用户审批」里通过。
    """
    result = users.register(
        username=payload.username,
        password=payload.password,
        display_name=payload.display_name,
    )
    if result["status"] == "error":
        return BaseResponse.error(result["message"])
    return BaseResponse.success(data={"status": result["status"]}, msg=result["message"])


@router.post("/login", summary="登录并签发 token")
def login(payload: LoginRequest, request: Request) -> BaseResponse:
    """校验用户名口令，通过后签发 JWT。

    失败一律返回 ``CODE_UNAUTHORIZED``，**不区分**"用户不存在"、"口令错误"
    与"账号尚未通过审批"——否则这个接口会变成用户名探测器。
    真正的失败原因只写进服务端日志。

    因此前端拿到的提示是统一的："用户名或密码错误，或账号尚未通过审批"。

    **限流与统一文案是两件事**：统一文案防的是**用户名枚举**（不让攻击者
    从提示区分账号是否存在），防不住**口令爆破**——爆破者不在乎提示，
    只在乎能不能反复试。所以这里额外按「用户名 + 来源 IP」计数，
    超过 ``LOGIN_MAX_ATTEMPTS`` 后锁定一段时间。
    """
    throttle = _throttle_key(payload.username, request)

    locked = login_locked_for(throttle)
    if locked > 0:
        logger.warning(
            "登录已被限流：%s（剩余 %d 秒）", payload.username, locked
        )
        # 文案与普通失败保持一致的**前缀**，但明确告知等待时间——
        # 否则用户会以为是口令错了，反复重试反而延长锁定。
        return BaseResponse.error(
            f"登录尝试过于频繁，请在 {locked} 秒后重试。",
            code=CODE_UNAUTHORIZED,
        )

    account = users.authenticate(payload.username, payload.password)
    if not account:
        count = register_login_failure(throttle)
        remaining = max(0, settings.login_max_attempts - count)
        if remaining:
            logger.info("登录失败（本窗口第 %d 次，剩余 %d 次）", count, remaining)
        else:
            logger.warning(
                "登录失败次数达到上限，锁定 %d 秒：%s",
                settings.login_lockout_seconds,
                payload.username,
            )
        return BaseResponse.error(
            "用户名或密码错误，或账号尚未通过管理员审批。", code=CODE_UNAUTHORIZED
        )

    # 成功即清零：否则正常用户偶尔输错几次会累积到被锁
    clear_login_failures(throttle)

    token, expires_in, jti = create_access_token(
        username=account["username"],
        display_name=account["display_name"],
        role=account["role"],
    )
    logger.info(
        "用户 %s 登录成功，已签发 token（%d 秒有效，jti=%s）",
        account["username"],
        expires_in,
        jti[:8],
    )
    return BaseResponse.success(
        data={
            **account,
            "access_token": token,
            "token_type": "bearer",
            "expires_in": expires_in,
        }
    )


@router.post("/logout", summary="登出（吊销当前 token）")
def logout(user: CurrentUser = Depends(get_current_user)) -> BaseResponse:
    """把当前令牌加入吊销名单，使其**立即失效**。

    为什么需要服务端登出：原来登出只是前端把 token 从 localStorage 删掉，
    而那个 JWT 在剩余有效期内**依然可用**。如果它被复制过（共享设备、
    浏览器历史、日志、抓包），"登出"没有任何保护作用。

    只吊销**当前这一个** ``jti``，不影响该用户在其他设备上的登录——
    全量作废会让用户"在手机上登出，把电脑也踢下线"。

    重复登出不报错（幂等）：网络重试、前端重复点击都会触发，
    第二次调用时令牌可能已过期/已吊销，此时 ``get_current_user`` 会先
    返回 401。所以这里对"已吊销"保持宽容。
    """
    if not user.jti:
        # 旧版令牌没有 jti（签发于本功能上线前），无法精确吊销。
        # 不静默成功——告诉调用方"这次没生效"，让它重新登录换新令牌。
        logger.warning("用户 %s 的 token 没有 jti，无法吊销", user.username)
        return BaseResponse.error(
            "该令牌不支持吊销（签发于本功能上线前），请重新登录。",
            code=CODE_UNAUTHORIZED,
        )

    import time as _time

    remaining = max(1, int(user.expires_at - _time.time())) if user.expires_at else 60
    revoke_token(user.jti, remaining)
    logger.info("用户 %s 已登出，令牌 %s 已吊销", user.username, user.jti[:8])
    return BaseResponse.success(data={"revoked": True}, msg="已登出。")


@router.get("/me", summary="当前登录用户")
def me(user: CurrentUser = Depends(get_current_user)) -> BaseResponse:
    """回显 token 里的身份。前端启动时用它校验本地 token 是否仍有效。"""
    return BaseResponse.success(
        data={
            "username": user.username,
            "display_name": user.display_name,
            "role": user.role,
            "is_admin": user.is_admin,
            "allow_admin_promotion": settings.allow_admin_promotion,
        }
    )
