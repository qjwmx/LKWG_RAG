"""用户与口令：注册、审批、登录校验。

三条设计取舍
------------
**1. 口令用 stdlib 的 pbkdf2_hmac，不引 passlib / bcrypt。**
需求只是"别存明文、校验别用等号"，``hashlib.pbkdf2_hmac`` 完全够用，
而 passlib 已久未维护、bcrypt 还要编译。存储格式是自描述的
``pbkdf2_sha256$迭代次数$盐$哈希``，将来换算法时按前缀分流即可。

**2. 管理员（root）不落 users 表。**
它由 .env 预置，``authenticate`` 里先于注册用户匹配。这样注册流程既覆盖不了
管理员账号，也没法抢注——把 root 放进同一张表，"第一个注册的人叫什么"
就变成了一个安全边界问题。

**3. 注册后必须管理员在后台界面审批通过才能登录。**
``authenticate`` 对**任何**失败原因都返回 ``None``（用户不存在 / 待审批 /
已拒绝 / 口令错），不区分——否则这个接口就成了"这个用户名存在吗"的探测器。
真正的失败原因只写进服务端日志。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timezone

from sqlalchemy import delete, select, update

from app.config import (
    ROLE_ADMIN,
    ROLE_USER,
    USER_STATUS_APPROVED,
    USER_STATUS_PENDING,
    USER_STATUS_REJECTED,
    PASSWORD_MIN_LENGTH,
    USERNAME_MAX_LENGTH,
    USERNAME_MIN_LENGTH,
    settings,
)
from app.db.models import UserRecord
from app.db.session import transaction

logger = logging.getLogger(__name__)

_ALGO = "pbkdf2_sha256"
# 260k 次是 OWASP 对 PBKDF2-HMAC-SHA256 的现行建议下限。
# 代价是每次登录约 100ms——登录不是高频操作，这个开销换暴力破解成本，值得。
_ITERATIONS = 260_000
_SALT_BYTES = 16


# --------------------------------------------------------------------------- 口令


def hash_password(password: str) -> str:
    """生成 ``算法$迭代次数$盐$哈希``。每次调用盐都不同。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "$".join(
        [
            _ALGO,
            str(_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """校验口令。存储串格式不对时返回 False，不抛异常。

    用 ``hmac.compare_digest`` 做定长时间比较——普通 ``==`` 会在首个不同字节处
    短路返回，理论上可被计时攻击逐字节猜出哈希。
    """
    try:
        algo, iterations, salt_b64, digest_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False

    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(iterations)
    )
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------- 校验


def validate_username(username: str) -> str | None:
    """返回错误说明；合法则返回 None。"""
    if len(username) < USERNAME_MIN_LENGTH:
        return f"用户名至少 {USERNAME_MIN_LENGTH} 个字符。"
    if len(username) > USERNAME_MAX_LENGTH:
        return f"用户名最多 {USERNAME_MAX_LENGTH} 个字符。"
    if any(ch.isspace() for ch in username):
        return "用户名不能包含空格。"
    return None


def validate_password(password: str) -> str | None:
    if len(password or "") < PASSWORD_MIN_LENGTH:
        return f"密码至少 {PASSWORD_MIN_LENGTH} 位。"
    return None


# --------------------------------------------------------------------------- 查询


def _to_public(record: UserRecord) -> dict:
    """对外返回的字段。**绝不含 password_hash。**"""
    return {
        "username": record.username,
        "display_name": record.display_name or record.username,
        "role": record.role,
        "status": record.status,
        "created_at": _fmt(record.created_at),
        "updated_at": _fmt(record.updated_at),
    }


def _fmt(value: datetime | None) -> str:
    if value is None:
        return ""
    # SQLite 存的是 naive UTC；直接格式化会让界面显示的时间整体偏 8 小时（且不报错）
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def get_user(username: str) -> dict | None:
    username = (username or "").strip()
    if not username:
        return None
    with transaction() as session:
        record = session.get(UserRecord, username)
        if record is None:
            return None
        data = _to_public(record)
        # 连同哈希一起带出供 authenticate 校验；调用方注意别往外传
        data["password_hash"] = record.password_hash
        return data


def list_users(status: str | None = None) -> list[dict]:
    stmt = select(UserRecord).order_by(UserRecord.created_at.desc())
    if status:
        stmt = stmt.where(UserRecord.status == status)
    with transaction() as session:
        records = session.execute(stmt).scalars().all()
        return [_to_public(r) for r in records]


def count_by_status() -> dict[str, int]:
    result: dict[str, int] = {
        USER_STATUS_PENDING: 0,
        USER_STATUS_APPROVED: 0,
        USER_STATUS_REJECTED: 0,
    }
    for row in list_users():
        result[row["status"]] = result.get(row["status"], 0) + 1
    return result


# --------------------------------------------------------------------------- 注册


def register(username: str, password: str, display_name: str = "") -> dict:
    """注册一个**待审批**用户。

    返回 ``{"status": "pending"|"error", "message": ...}``。
    """
    username = (username or "").strip()
    display_name = (display_name or "").strip() or username

    for problem in (validate_username(username), validate_password(password)):
        if problem:
            return {"status": "error", "message": problem}

    if username == settings.admin_username:
        return {"status": "error", "message": "该用户名为系统保留，请换一个。"}

    existing = get_user(username)
    if existing:
        if existing["status"] == USER_STATUS_APPROVED:
            return {"status": "error", "message": "该用户名已被注册，请换一个。"}
        if existing["status"] == USER_STATUS_PENDING:
            return {
                "status": "error",
                "message": "该用户名已提交过申请，请等待管理员审批。",
            }

        # 曾被拒绝：复用同一行、重置口令并回到 pending。
        # 不删行是为了让对方重新申请时能复用，而不是"账号凭空消失、同名再也注册不了"。
        with transaction() as session:
            session.execute(
                update(UserRecord)
                .where(UserRecord.username == username)
                .values(
                    password_hash=hash_password(password),
                    display_name=display_name,
                    status=USER_STATUS_PENDING,
                    updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
                )
            )
        logger.info("用户 %s 重新提交注册申请", username)
        return {"status": "pending", "message": "已重新提交申请，请等待管理员审批。"}

    with transaction() as session:
        session.add(
            UserRecord(
                username=username,
                password_hash=hash_password(password),
                display_name=display_name,
                role=ROLE_USER,
                status=USER_STATUS_PENDING,
            )
        )
    logger.info("新用户注册：%s（待审批）", username)
    return {"status": "pending", "message": "注册申请已提交，请等待管理员审批。"}


# --------------------------------------------------------------------------- 登录


def authenticate(username: str, password: str) -> dict | None:
    """校验登录。成功返回 ``{username, display_name, role}``，失败一律 None。

    管理员（root）先匹配：它来自 .env，不在 users 表里。
    """
    username = (username or "").strip()
    if not username or not password:
        return None

    if username == settings.admin_username:
        if hmac.compare_digest(
            password.encode("utf-8"), settings.admin_password.encode("utf-8")
        ):
            return {
                "username": settings.admin_username,
                "display_name": settings.admin_display_name,
                "role": ROLE_ADMIN,
            }
        logger.warning("管理员 %s 口令错误", username)
        return None

    record = get_user(username)
    if not record:
        logger.info("登录失败：用户 %s 不存在", username)
        return None
    if record["status"] != USER_STATUS_APPROVED:
        # 待审批 / 已拒绝都走这里。前端只会看到统一的失败文案，
        # 具体状态留给服务端日志。
        logger.info("登录失败：用户 %s 状态为 %s", username, record["status"])
        return None
    if not verify_password(password, record.get("password_hash", "")):
        logger.warning("用户 %s 口令错误", username)
        return None

    return {
        "username": record["username"],
        "display_name": record["display_name"],
        "role": record["role"],
    }


# --------------------------------------------------------------------------- 审批与角色


def set_user_status(username: str, status: str) -> dict:
    """管理员审批：把用户置为 approved / rejected / pending。"""
    if status not in (USER_STATUS_APPROVED, USER_STATUS_REJECTED, USER_STATUS_PENDING):
        return {"status": "error", "message": f"未知状态 {status!r}。"}

    username = (username or "").strip()
    if username == settings.admin_username:
        return {"status": "error", "message": "管理员账号不可修改。"}

    if not get_user(username):
        return {"status": "error", "message": f"用户 {username} 不存在。"}

    with transaction() as session:
        session.execute(
            update(UserRecord)
            .where(UserRecord.username == username)
            .values(status=status, updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
        )

    label = {
        USER_STATUS_APPROVED: "已通过审批",
        USER_STATUS_REJECTED: "已拒绝",
        USER_STATUS_PENDING: "已重置为待审批",
    }[status]
    logger.info("管理员把用户 %s 置为 %s", username, status)
    return {"status": "success", "message": f"用户 {username} {label}。"}


def set_user_role(username: str, role: str) -> dict:
    """提升/降级用户角色。

    **root 自身不可被降级**——否则一次误操作就能把系统锁死（再没有管理员
    能进后台把它改回来）。root 也不在 users 表里，本就改不到，这里是显式挡一道。
    """
    if role not in (ROLE_ADMIN, ROLE_USER):
        return {"status": "error", "message": f"未知角色 {role!r}。"}

    username = (username or "").strip()
    if username == settings.admin_username:
        return {"status": "error", "message": "管理员（root）账号的角色不可修改。"}
    if not settings.allow_admin_promotion:
        return {
            "status": "error",
            "message": "当前配置不允许变更管理员角色（ALLOW_ADMIN_PROMOTION=false）。",
        }

    record = get_user(username)
    if not record:
        return {"status": "error", "message": f"用户 {username} 不存在。"}
    if record["status"] != USER_STATUS_APPROVED:
        return {
            "status": "error",
            "message": "只有已通过审批的用户才能变更角色。",
        }

    with transaction() as session:
        session.execute(
            update(UserRecord)
            .where(UserRecord.username == username)
            .values(role=role, updated_at=datetime.now(timezone.utc).replace(tzinfo=None))
        )
    label = "管理员" if role == ROLE_ADMIN else "普通用户"
    logger.info("管理员把用户 %s 的角色改为 %s", username, role)
    return {"status": "success", "message": f"用户 {username} 已设为{label}。"}


def delete_user(username: str) -> dict:
    """删除注册用户（管理员用）。root 不可删。"""
    username = (username or "").strip()
    if username == settings.admin_username:
        return {"status": "error", "message": "管理员（root）账号不可删除。"}
    if not get_user(username):
        return {"status": "error", "message": f"用户 {username} 不存在。"}

    with transaction() as session:
        session.execute(delete(UserRecord).where(UserRecord.username == username))
    logger.info("已删除用户 %s", username)
    return {"status": "success", "message": f"已删除用户 {username}。"}
