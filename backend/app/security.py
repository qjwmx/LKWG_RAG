"""JWT 鉴权与请求身份上下文。

为什么身份必须来自 token
------------------------
如果 ``username`` / ``role`` / ``owner`` / ``scope`` 由前端在请求里传、后端照单
全收，那么"规则由服务端强制"就是假的——"你是谁"是客户端**声称**的，直接调接口
就能自称管理员。本模块把身份来源换成服务端签发、服务端校验的 JWT：调用方只出示
凭据，身份由后端解出。

管理员（root）
-------------
root 由 .env 预置、不落 users 表。它在 token 里同样是 ``role="admin"``，
但**它绕过所有权与可见范围检查**（跨用户全量可见可管），见各 service 的
``include_all`` 参数。

为什么失败返回 HTTP 401 而不是 200+业务码
-----------------------------------------
项目约定是"HTTP 恒为 200，业务状态放 body.code"，唯一的例外就是鉴权。
客户端据此能把"凭据问题"与"业务错误"分开处理，不必为鉴权单独写一套判断。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, Request, status

from app.cache import is_token_revoked
from app.config import ROLE_ADMIN, ROLE_USER, settings
from app.observability import set_username

logger = logging.getLogger(__name__)

# 模块加载时取一次并缓存。
# 不能每次调用都读 settings.effective_jwt_secret——未配置时那个 property 每次都
# 生成新的随机密钥，同一个进程里前后签发的 token 会互不认账。
_JWT_SECRET = settings.effective_jwt_secret


@dataclass(frozen=True)
class CurrentUser:
    """一次请求的身份，**只可能来自已校验的 JWT**。

    业务代码把它当作"当前是谁"的唯一依据，不要再从请求体或 query 里读身份字段。
    """

    username: str
    display_name: str
    role: str
    # 令牌唯一标识。**登出要用它**：知道"是哪个令牌"才能只吊销这一个，
    # 而不是把该用户的全部令牌一起作废（那会让其他设备被踢下线）。
    jti: str = ""
    # 令牌到期时间（UTC 时间戳）。吊销名单的 TTL 用它算，避免名单无限增长。
    expires_at: int = 0

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


def create_access_token(
    username: str, display_name: str, role: str
) -> tuple[str, int, str]:
    """签发 token，返回 ``(token, 有效期秒数, jti)``。

    只放最小必要信息：``sub`` 是用户名、``role`` 决定权限。不放口令哈希、
    不放数据库主键——token 是给客户端看的，能少放就少放。

    ``jti``（JWT ID）是**吊销的唯一依据**：登出时把这个 id 记进名单，
    校验时命中即拒。没有它就只能等令牌自然过期。
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=settings.jwt_expire_minutes)
    jti = uuid.uuid4().hex
    payload = {
        "sub": username,
        "name": display_name,
        "role": role,
        "jti": jti,
        "iat": now,
        "exp": expires_at,
    }
    token = jwt.encode(payload, _JWT_SECRET, algorithm=settings.jwt_algorithm)
    return token, settings.jwt_expire_minutes * 60, jti


def decode_access_token(token: str) -> CurrentUser | None:
    """校验并解出身份。**任何失败都返回 None**，不抛异常。

    过期与非法分开记日志：前者是正常现象（会话挂太久），后者可能是攻击，
    排查时值得区分。

    **已吊销的令牌也算失败**：登出后旧 token 必须立刻不可用，
    否则"登出"只是前端把本地副本删了，凭据本身仍然有效。
    """
    try:
        payload = jwt.decode(token, _JWT_SECRET, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        logger.info("token 已过期，需要重新登录")
        return None
    except jwt.InvalidTokenError as exc:
        logger.warning("token 校验失败：%s", exc)
        return None

    username = (payload.get("sub") or "").strip()
    if not username:
        logger.warning("token 里没有 sub，视为无效")
        return None

    jti = (payload.get("jti") or "").strip()
    if is_token_revoked(jti):
        logger.info("token 已被吊销（用户 %s 已登出）", username)
        return None

    return CurrentUser(
        username=username,
        display_name=payload.get("name") or username,
        role=payload.get("role") or ROLE_USER,
        jti=jti,
        expires_at=int(payload.get("exp") or 0),
    )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return token.strip()


def get_current_user(request: Request) -> CurrentUser:
    """FastAPI 依赖：解出当前用户，失败抛 401。"""
    token = _bearer_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Authorization: Bearer <token>，请先登录。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = decode_access_token(token)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="凭据无效或已过期，请重新登录。",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 把身份写进可观测性上下文。**这里是唯一能确定身份的地方**——
    # 身份只来自校验过的 JWT。中间件刻意不自己解析 token：
    # 那会把鉴权逻辑复制成两份，早晚不一致（而鉴权不一致是安全问题）。
    set_username(user.username)
    return user


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """FastAPI 依赖：要求管理员，非管理员返回 403。

    403 而不是 401：身份是有效的，只是权限不够。
    """
    if not user.is_admin:
        logger.warning("用户 %s 尝试执行管理员操作", user.username)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该操作需要管理员权限。",
        )
    return user
