"""三块共享状态：令牌吊销、登录限流、查询向量缓存。

统一约定
--------
每个功能都提供 **Redis 实现 + 进程内实现**，由 ``get_client()`` 是否
可用决定走哪条。调用方只看到一组函数，不关心后端。

**为什么必须双实现**：Redis 是可选增强（见 ``client.py``）。只有 Redis
实现的话，没配 Redis 的环境会直接报错——那就不是"可选"了。

进程内实现的两个硬要求
----------------------
1. **线程安全**：FastAPI 把 ``def`` 端点丢进线程池，同一进程里并发访问
   这些结构是常态。所有读写都加锁。
2. **有界**：进程内缓存不能无限增长。攻击者可以用大量不同的用户名
   把限流表撑爆内存。所以用带容量上限的 LRU。

令牌吊销为什么要 ``jti``
------------------------
原来登出是**纯前端**的（``localStorage.removeItem``），服务端签发的
JWT 在 24 小时内**依然有效**。这意味着"登出"后旧 token 还能用——
如果它被复制过（浏览器历史、日志、共享设备），登出这个动作没有任何
保护作用。加上 ``jti`` 与吊销名单后，登出才真正让令牌失效。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict

import numpy as np

from app.cache.client import get_client, mark_unavailable
from app.config import settings

logger = logging.getLogger(__name__)

# 进程内缓存容量上限。超过后按 LRU 淘汰。
# 取值理由：单条记录都很小（令牌 id 几十字节、向量几 KB），
# 几千条也只有几 MB，不会造成内存压力，同时足以覆盖单机日常流量。
_MAX_ENTRIES = 4096


def _key(*parts: str) -> str:
    prefix = (settings.redis_key_prefix or "myrag").strip() or "myrag"
    return ":".join([prefix, *parts])


def _safe(call, default):
    """执行一次 Redis 操作，失败时降级。

    Redis 是**加速器**：任何异常都不该让业务失败，所以统一吞掉并
    标记不可用（进入冷却期）。返回 ``default`` 让调用方走降级路径。
    """
    client = get_client()
    if client is None:
        return default
    try:
        return call(client)
    except Exception as exc:  # noqa: BLE001 - 缓存故障不该影响业务
        mark_unavailable(exc)
        return default


# --------------------------------------------------------------------------- 通用 LRU


class _Lru:
    """带容量上限的进程内字典（LRU 淘汰）。**线程安全**。"""

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._data: OrderedDict[str, tuple[float, object]] = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at and expires_at < time.time():
                # 惰性过期：读时才发现过期，省一个后台清理线程
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value, ttl: int) -> None:
        expires_at = time.time() + ttl if ttl else 0.0
        with self._lock:
            self._data[key] = (expires_at, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def size(self) -> int:
        """当前条目数（含已过期但尚未被惰性清理的）。

        加锁读：``stats()`` 可能被诊断接口并发调用，直接读 ``_data``
        会在字典被改写时抛 ``RuntimeError``。
        """
        with self._lock:
            return len(self._data)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# --------------------------------------------------------------------------- 令牌吊销

_denylist = _Lru()
_login_failures = _Lru()
_embed_cache = _Lru()
_search_cache = _Lru()

# 进程内语料版本号。Redis 不可用时用它兜底——单进程部署下同样能正确失效。
_local_corpus_version = "0"


def revoke_token(jti: str, expires_in: int) -> bool:
    """把一个令牌 id 加入吊销名单，存活到它自然过期为止。

    ``expires_in`` 必须给足：令牌过期后名单里的记录就没有意义了
    （JWT 自己会因为 ``exp`` 被拒），所以 TTL 设成剩余有效期即可，
    这样名单不会无限增长。
    """
    if not jti:
        return False
    ttl = max(1, int(expires_in))
    ok = _safe(lambda c: c.setex(_key("revoked", jti), ttl, b"1"), None)
    # **本地也记一份**：即使 Redis 不可用，单进程内吊销也要生效。
    # 反过来（只在 Redis 记）会让"没配 Redis"的环境完全失去吊销能力。
    _denylist.set(jti, True, ttl)
    return ok is not None


def is_token_revoked(jti: str) -> bool:
    """令牌是否已被吊销。

    先查本地再查 Redis：本地命中就不用发请求（登出与后续请求通常
    在同一个进程），Redis 查不到时以本地为准。

    **这是 fail-open 的，且是刻意的取舍。** Redis 不可用时，查不到
    吊销记录 → 判定"未吊销" → 令牌继续可用。反过来（fail-closed，
    查不到就拒绝）会让 Redis 一挂**所有人立刻掉线**——用一个安全
    增强功能换掉整个站点的可用性。

    代价要说清楚：Redis 宕机 + 多实例部署时，在别的实例上登出的令牌
    会在本实例继续有效，直到 Redis 恢复或令牌自然过期。
    单进程部署没有这个缺口（本地名单就是全量）。
    """
    if not jti:
        return False
    if _denylist.get(jti):
        return True
    result = _safe(lambda c: c.exists(_key("revoked", jti)), None)
    if result:
        return True
    return False


# --------------------------------------------------------------------------- 登录限流


def _client_fingerprint(identifier: str) -> str:
    """对标识做哈希后再入键。

    直接拿用户名当键会把"谁在尝试登录"写进 Redis——运维能看到
    Redis 里有哪些用户名。哈希后仍然能限流，但键里不含明文标识。
    """
    digest = hashlib.sha256((identifier or "").encode("utf-8")).hexdigest()
    return digest[:32]


def register_login_failure(identifier: str) -> int:
    """记一次登录失败，返回**当前窗口内的失败次数**。

    ``identifier`` 由调用方组合（用户名 + 来源 IP），见 ``routes/auth.py``。

    达到上限时**额外**写一个锁定键，TTL 是 ``LOGIN_LOCKOUT_SECONDS``。
    用两个键而不是一个，是因为"统计窗口"和"锁定时长"是两个独立的配置：
    单个键的 TTL 只能是其中之一，另一个设置就会**静默失效**
    （用户改了配置却不生效，是最难排查的一类问题）。
    """
    if not identifier:
        return 0
    digest = _client_fingerprint(identifier)
    key = _key("login_fail", digest)
    lock_key = _key("login_lock", digest)
    window = max(1, int(settings.login_window_seconds))
    limit = max(1, int(settings.login_max_attempts))
    lockout = max(1, int(settings.login_lockout_seconds))

    def _incr(client):
        # pipeline 保证 INCR 与 EXPIRE 一起发送，避免"设了计数却忘了过期"
        # 导致该用户被永久锁定。
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, window)
        count = int(pipe.execute()[0])
        if count >= limit:
            # 达到上限即开始锁定。重复触发只是续期，无副作用。
            client.setex(lock_key, lockout, b"1")
        return count

    count = _safe(_incr, None)
    if count is None:
        # 降级：本地计数 + 本地锁定
        current = int(_login_failures.get(key) or 0) + 1
        _login_failures.set(key, current, window)
        if current >= limit:
            _login_failures.set(lock_key, True, lockout)
        return current
    return count


def login_failure_count(identifier: str) -> int:
    """当前窗口内的失败次数（不增加）。"""
    if not identifier:
        return 0
    key = _key("login_fail", _client_fingerprint(identifier))
    count = _safe(lambda c: int(c.get(key) or 0), None)
    if count is None:
        return int(_login_failures.get(key) or 0)
    return count


def clear_login_failures(identifier: str) -> None:
    """登录成功后清零（计数与锁定一起清）。

    否则正常用户偶尔输错几次会累积到被锁。
    """
    if not identifier:
        return
    digest = _client_fingerprint(identifier)
    key = _key("login_fail", digest)
    lock_key = _key("login_lock", digest)
    _safe(lambda c: c.delete(key, lock_key), None)
    _login_failures.delete(key)
    _login_failures.delete(lock_key)


def login_locked_for(identifier: str) -> int:
    """还需锁定多少秒；未锁定返回 0。"""
    if not identifier:
        return 0
    digest = _client_fingerprint(identifier)
    lock_key = _key("login_lock", digest)

    ttl = _safe(lambda c: int(c.ttl(lock_key)), None)
    if ttl is None:
        # 降级路径：本地表不提供 TTL 查询，用锁定时长保守估计。
        # 保守（可能多报几秒）比漏报安全——漏报会让锁定形同虚设。
        if _login_failures.get(lock_key):
            return max(1, int(settings.login_lockout_seconds))
        return 0
    # Redis 对不存在的键返回 -2、无 TTL 返回 -1，都当作"未锁定"
    return max(0, ttl)


# --------------------------------------------------------------------------- 查询向量缓存


def _embed_key(model: str, text: str) -> str:
    """缓存键 = 模型标识 + 文本哈希。

    **必须把模型算进键里**。只用文本哈希的话，切换 ``EMBEDDING_MODEL``
    后会命中旧模型算出的向量——维度可能不同（直接导致检索崩溃或静默
    返回错结果），而且**不报错**，只是"换模型后检索变差了"。
    这是缓存最典型的隐蔽 bug。
    """
    digest = hashlib.sha256((text or "").encode("utf-8")).hexdigest()
    return _key("embed", model, digest)


def get_cached_embedding(model: str, text: str) -> list[float] | None:
    """取缓存的查询向量。未命中返回 ``None``。"""
    if not text:
        return None
    key = _embed_key(model, text)

    cached = _embed_cache.get(key)
    if cached is not None:
        return list(cached)

    raw = _safe(lambda c: c.get(key), None)
    if not raw:
        return None
    try:
        # 存的是 float32 原始字节：比 JSON 小一半以上，且无精度损失
        vector = np.frombuffer(raw, dtype=np.float32)
    except (ValueError, TypeError):
        return None
    if vector.size == 0:
        return None
    result = vector.tolist()
    _embed_cache.set(key, result, int(settings.embed_cache_ttl))
    return result


def cache_embedding(model: str, text: str, vector: list[float]) -> None:
    """写入查询向量缓存。"""
    if not text or not vector:
        return
    ttl = max(1, int(settings.embed_cache_ttl))
    key = _embed_key(model, text)

    payload = np.asarray(vector, dtype=np.float32).tobytes()
    _safe(lambda c: c.setex(key, ttl, payload), None)
    _embed_cache.set(key, list(vector), ttl)


# --------------------------------------------------------------------------- 检索结果缓存


def corpus_version() -> str:
    """当前语料版本号。

    任何"会改变检索结果"的写入都必须调 ``bump_corpus_version``，
    让所有缓存键失效。**这是正确性的关键**，不是优化：

    检索结果依赖库里的内容。若只用「查询文本」做键，用户上传一份
    新文档后，同一个问题在 TTL 内仍然命中旧结果——表现为"上传成功但
    搜不到新内容"，而且**不报错**，最难排查的那类问题。
    """
    version = _safe(lambda c: c.get(_key("corpus_version")), None)
    if version:
        return version.decode("utf-8", errors="replace") if isinstance(version, bytes) else str(version)
    return _local_corpus_version


def bump_corpus_version() -> str:
    """语料变更时调用：让所有检索结果缓存立即失效。

    文档上传、删除、重建索引都要调。用 ``INCR`` 而不是 ``SET 时间戳``：
    单调递增，不会因为机器时钟回拨而产生"回到旧版本号"的情况
    （那会让旧缓存重新生效）。
    """
    global _local_corpus_version
    value = _safe(lambda c: int(c.incr(_key("corpus_version"))), None)
    if value is None:
        # 降级：本地版本号。单进程下同样有效。
        _local_corpus_version = str(int(_local_corpus_version) + 1)
        value = int(_local_corpus_version)
    else:
        # 本地也跟着走，这样 Redis 掉线时两边不会差得太远
        _local_corpus_version = str(value)
    return str(value)


def _search_key(
    query: str,
    category: str,
    requester: str,
    include_all: bool,
    top_k: int,
    version: str,
) -> str:
    """检索缓存的键。

    ★ **必须包含可见性三要素**（``requester`` / ``include_all`` / 语料版本）。
    只按查询文本做键会造成**越权泄露**：

    - root（``include_all=True``）检索时能看到所有人的私有文档；
      若结果被普通用户命中，他就拿到了别人的私有内容。
    - 普通用户 A 的私有文档也会出现在他自己的检索结果里；
      若 B 用同样的词，就会命中 A 的结果。

    这类漏洞不会报错、不会 500，只是"结果里多了些不该看到的材料"，
    所以必须在键的设计上就堵死，而不是靠事后过滤。

    语料版本进键是为了让上传/删除后旧缓存立即失效（见 ``corpus_version``）。
    """
    # 可见性身份做哈希：键里不该出现明文用户名
    who = hashlib.sha256(
        f"{requester}|{'all' if include_all else 'self'}".encode("utf-8")
    ).hexdigest()[:24]
    q = hashlib.sha256((query or "").encode("utf-8")).hexdigest()
    return _key("search", version, who, category or "全部", str(int(top_k)), q)


def get_cached_search(
    query: str,
    category: str,
    requester: str,
    include_all: bool,
    top_k: int,
) -> list[dict] | None:
    """取缓存的检索结果。未命中返回 ``None``。"""
    if not query:
        return None
    key = _search_key(query, category, requester, include_all, top_k, corpus_version())

    cached = _search_cache.get(key)
    if cached is not None:
        return [dict(hit) for hit in cached]

    raw = _safe(lambda c: c.get(key), None)
    if not raw:
        return None
    try:
        hits = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    if not isinstance(hits, list):
        return None
    _search_cache.set(key, hits, int(settings.search_cache_ttl))
    return [dict(hit) for hit in hits]


def cache_search(
    query: str,
    category: str,
    requester: str,
    include_all: bool,
    top_k: int,
    hits: list[dict],
) -> None:
    """写入检索结果缓存。

    **空结果也缓存**：``count_searchable`` 预检说"有资料"但检索返回空，
    是真实会发生的（分类过滤掉全部候选）。缓存空结果能让这种查询
    不再每次都扫 7MB 向量。
    """
    if not query:
        return
    ttl = max(1, int(settings.search_cache_ttl))
    key = _search_key(query, category, requester, include_all, top_k, corpus_version())

    try:
        payload = json.dumps(hits, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return
    _safe(lambda c: c.setex(key, ttl, payload), None)
    _search_cache.set(key, [dict(hit) for hit in hits], ttl)


# --------------------------------------------------------------------------- 维护


def reset_local_state() -> None:
    """清空进程内缓存。测试用。"""
    global _local_corpus_version
    _denylist.clear()
    _login_failures.clear()
    _embed_cache.clear()
    _search_cache.clear()
    _local_corpus_version = "0"


def stats() -> dict:
    """各缓存的规模，供健康检查/调试查看。"""
    client = get_client()
    info: dict = {
        "redis": client is not None,
        "corpus_version": corpus_version(),
        "local_entries": {
            "revoked": _denylist.size(),
            "login_fail": _login_failures.size(),
            "embed": _embed_cache.size(),
            "search": _search_cache.size(),
        },
    }
    if client is not None:
        try:
            info["keys"] = int(client.dbsize())
        except Exception:  # noqa: BLE001
            info["keys"] = -1
    return info
