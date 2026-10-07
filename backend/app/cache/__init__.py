"""Redis 缓存层。

四块共享状态，每块都有 **Redis 实现 + 进程内降级实现**：

- ``store.revoke_token`` / ``is_token_revoked`` —— 令牌吊销（登出真正生效）
- ``store.register_login_failure`` 等 —— 登录限流（防口令爆破）
- ``store.get_cached_embedding`` / ``cache_embedding`` —— 查询向量缓存
- ``store.get_cached_search`` / ``cache_search`` —— 检索结果缓存
  （靠 ``corpus_version`` 在文档变更时整体失效）

**Redis 是可选的**：``REDIS_URL`` 留空时全部走进程内实现，功能不缺失，
只是不跨进程共享。详见 ``client.py`` 与 ``store.py`` 的模块说明。
"""

from app.cache.client import (
    get_client,
    health,
    is_configured,
    reset_client,
    set_client_for_testing,
)
from app.cache.store import (
    bump_corpus_version,
    cache_embedding,
    cache_search,
    clear_login_failures,
    corpus_version,
    get_cached_embedding,
    get_cached_search,
    is_token_revoked,
    login_failure_count,
    login_locked_for,
    register_login_failure,
    reset_local_state,
    revoke_token,
    stats,
)

__all__ = [
    "bump_corpus_version",
    "cache_embedding",
    "cache_search",
    "clear_login_failures",
    "corpus_version",
    "get_cached_embedding",
    "get_cached_search",
    "get_client",
    "health",
    "is_configured",
    "is_token_revoked",
    "login_failure_count",
    "login_locked_for",
    "register_login_failure",
    "reset_client",
    "reset_local_state",
    "revoke_token",
    "set_client_for_testing",
    "stats",
]
