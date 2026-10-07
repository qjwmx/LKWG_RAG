"""Redis 连接与降级。

设计前提
--------
本项目的核心承诺是**"空环境三条命令起服务"**，所以 Redis 是**可选增强**，
不是依赖：

- ``REDIS_URL`` 留空 → 全部功能走进程内实现，**功能不缺失**，
  只是不跨进程/不跨重启共享（见 ``store.py``）。
- ``REDIS_URL`` 填了但连不上 → 同样降级到进程内，并记一次日志。

**为什么必须降级而不是报错**：Redis 挂掉时让整个站点 500 是拿缓存
换可用性。缓存的意义是加速，不该把故障传导给业务。

熔断（circuit breaker）
----------------------
``REDIS_SOCKET_TIMEOUT`` 默认 1 秒。如果 Redis 真的挂了，**每个请求**
都等 1 秒才降级——比不用缓存还慢一个数量级，而且线程池会被拖垮。
所以失败后进入冷却期（``_COOLDOWN_SECONDS``），冷却期内直接判定不可用、
不发请求。这样 Redis 宕机时业务只损失"第一次请求的 1 秒"。
"""

from __future__ import annotations

import logging
import time

from app.config import settings

logger = logging.getLogger(__name__)

# 连接失败后的冷却秒数。期间不再尝试连接，直接走降级路径。
_COOLDOWN_SECONDS = 30.0

_client = None
_override = None
_unavailable_until = 0.0
_logged_failure = False


def reset_client() -> None:
    """丢掉缓存连接。测试与配置热更新用。"""
    global _client, _unavailable_until, _logged_failure
    _client = None
    _unavailable_until = 0.0
    _logged_failure = False


def set_client_for_testing(client) -> None:
    """测试注入（fakeredis 或替身）。

    传 ``None`` 表示恢复"按配置建连接"的行为。
    """
    global _override, _unavailable_until
    _override = client
    _unavailable_until = 0.0


def is_configured() -> bool:
    return bool((settings.redis_url or "").strip()) or _override is not None


def mark_unavailable(exc: Exception | None = None) -> None:
    """标记 Redis 不可用并进入冷却期。

    由调用方在捕获到 Redis 异常时调用。**不在每次失败时都打日志**——
    Redis 挂掉时那会变成日志洪水，反而掩盖真正的问题；只在状态
    "从可用变为不可用"时记一次。
    """
    global _unavailable_until, _logged_failure
    _unavailable_until = time.monotonic() + _COOLDOWN_SECONDS
    if not _logged_failure:
        _logged_failure = True
        logger.warning(
            "Redis 不可用，%.0f 秒内降级到进程内实现（不影响功能，只是不跨进程共享）：%s",
            _COOLDOWN_SECONDS,
            exc or "连接失败",
        )


def get_client():
    """返回 Redis 客户端；不可用或未配置时返回 ``None``。

    调用方**必须**能处理 ``None``——这是降级路径的入口。
    """
    global _client, _unavailable_until, _logged_failure

    if _override is not None:
        return _override
    if not is_configured():
        return None
    if time.monotonic() < _unavailable_until:
        return None
    if _client is not None:
        return _client

    try:
        import redis
    except ImportError:  # pragma: no cover - 依赖没装时的兜底
        mark_unavailable(Exception("未安装 redis 包"))
        return None

    try:
        client = redis.Redis.from_url(
            settings.redis_url,
            socket_timeout=settings.redis_socket_timeout,
            socket_connect_timeout=settings.redis_socket_timeout,
            # **不自动解码**：嵌入向量要以原始 float32 字节存取，
            # decode_responses=True 会把二进制读成乱码字符串。
            # 需要字符串的地方（键名、令牌 id）显式 decode。
            decode_responses=False,
            health_check_interval=30,
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001 - 任何连接问题都降级
        mark_unavailable(exc)
        return None

    _client = client
    _logged_failure = False
    logger.info("Redis 已连接：%s", _redact(settings.redis_url))
    return _client


def _redact(url: str) -> str:
    """日志里隐去口令。

    ``redis://user:password@host:6379/0`` 直接打进日志等于把口令写进
    日志文件——而日志常被收集、转发、长期保留。
    """
    text = (url or "").strip()
    if "@" not in text:
        return text
    scheme, _, rest = text.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


def health() -> tuple[bool, str]:
    """健康检查用。返回 ``(是否可用, 说明)``，不抛异常。"""
    if not is_configured():
        return True, "未配置 REDIS_URL，使用进程内缓存（单进程部署足够）"
    client = get_client()
    if client is None:
        return False, (
            f"配置了 REDIS_URL 但连不上（{_redact(settings.redis_url)}），"
            "已降级到进程内缓存：功能正常，但不跨进程共享，"
            "多实例部署下登录限流与令牌吊销会各自为政。"
        )
    try:
        client.ping()
    except Exception as exc:  # noqa: BLE001
        mark_unavailable(exc)
        return False, f"Redis ping 失败（{type(exc).__name__}）：已降级到进程内缓存"
    return True, f"已连接（{_redact(settings.redis_url)}）"
