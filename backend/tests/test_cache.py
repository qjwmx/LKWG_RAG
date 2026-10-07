"""Redis 缓存层、令牌吊销、登录限流、查询向量与检索结果缓存的测试。

**全部用 fakeredis，不依赖真实 Redis 服务**（夹具见 conftest 的
``_isolated_cache``）。真实 Redis 的验证另有一组手工探测，见 README。

五组：
1. **降级** —— Redis 挂了/没配时功能必须照常，且不能拖慢请求
2. **令牌吊销** —— 登出后旧 token 立刻不可用
3. **登录限流** —— 爆破被挡，且不误伤其他账号
4. **向量缓存** —— 命中/失效边界（模型、文本、维度）
5. **检索结果缓存** —— ★ 可见性隔离（不能越权）+ 语料变更失效
"""

from __future__ import annotations

import time

import pytest

from app.cache import client as cache_client
from app.cache import store as cache_store


# --------------------------------------------------------------------------- 键名与前缀


def test_keys_are_prefixed():
    """键必须带前缀，否则同一台 Redis 上多环境会互相污染。"""
    from app.config import settings

    settings.redis_key_prefix = "myrag"
    assert cache_store._key("revoked", "abc") == "myrag:revoked:abc"


def test_client_fingerprint_hides_username():
    """限流键里不能出现明文用户名。

    直接拿 ``alice|1.2.3.4`` 当键，运维在 Redis 里就能看到谁在尝试登录。
    哈希后仍然能限流，但键里不含明文标识。
    """
    digest = cache_store._client_fingerprint("alice|1.2.3.4")
    assert "alice" not in digest
    assert "1.2.3.4" not in digest
    # 确定性：同一标识每次都要得到同一个键，否则限流形同虚设
    assert digest == cache_store._client_fingerprint("alice|1.2.3.4")
    assert digest != cache_store._client_fingerprint("bob|1.2.3.4")


# --------------------------------------------------------------------------- 令牌吊销


def test_revoke_then_detected():
    cache_store.revoke_token("jti-1", 60)
    assert cache_store.is_token_revoked("jti-1") is True
    assert cache_store.is_token_revoked("jti-2") is False


def test_revoked_token_is_shared_across_processes():
    """吊销必须写进 Redis，不能只在进程内。

    多实例部署时，在 A 实例登出的令牌必须让 B 实例也拒绝——
    这正是"进程内实现不够用、需要 Redis"的核心场景。
    """
    cache_store.revoke_token("shared", 60)

    # 模拟另一个进程：清掉进程内缓存，只留 Redis
    cache_store.reset_local_state()
    assert cache_store._denylist.get("shared") is None, "本地缓存应已清空"

    assert cache_store.is_token_revoked("shared") is True


def test_revoke_without_jti_is_ignored():
    """空 jti 不该写进名单（否则会误伤所有没有 jti 的旧令牌）。"""
    assert cache_store.revoke_token("", 60) is False
    assert cache_store.is_token_revoked("") is False


# --------------------------------------------------------------------------- 登录限流


def test_login_failures_accumulate_and_lock():
    for expected in range(1, 4):
        count = cache_store.register_login_failure("u|ip")
        assert count == expected
        if expected < 3:
            assert cache_store.login_locked_for("u|ip") == 0


def test_lockout_triggers_at_limit(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "login_max_attempts", 3)
    monkeypatch.setattr(settings, "login_lockout_seconds", 120)

    for _ in range(3):
        cache_store.register_login_failure("u|ip")

    locked = cache_store.login_locked_for("u|ip")
    assert locked > 0, "达到上限后必须处于锁定状态"
    assert locked <= 120


def test_lockout_uses_its_own_setting_not_the_window(monkeypatch):
    """锁定时长必须来自 ``LOGIN_LOCKOUT_SECONDS``，不能等于统计窗口。

    早先的实现只给计数键设 TTL（= 统计窗口），于是
    ``LOGIN_LOCKOUT_SECONDS`` **静默失效**——用户改了配置却不生效。
    所以这里让两个值明显不同，并断言用的是锁定值。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "login_max_attempts", 2)
    monkeypatch.setattr(settings, "login_window_seconds", 9999)
    monkeypatch.setattr(settings, "login_lockout_seconds", 30)

    cache_store.register_login_failure("u|ip")
    cache_store.register_login_failure("u|ip")

    locked = cache_store.login_locked_for("u|ip")
    assert 0 < locked <= 30, f"锁定应约 30 秒（锁定值），实际 {locked}（疑似用了窗口值）"


def test_clear_resets_both_count_and_lock(monkeypatch):
    """登录成功要同时清掉计数与锁定。

    只清计数的话，锁定键还在，用户仍然进不来——"密码对了却登不上"。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "login_max_attempts", 2)
    for _ in range(2):
        cache_store.register_login_failure("u|ip")
    assert cache_store.login_locked_for("u|ip") > 0

    cache_store.clear_login_failures("u|ip")
    assert cache_store.login_failure_count("u|ip") == 0
    assert cache_store.login_locked_for("u|ip") == 0


def test_different_identifiers_are_independent(monkeypatch):
    """限流按「用户名 + IP」隔离，不能一人的失败连累其他人。"""
    from app.config import settings

    monkeypatch.setattr(settings, "login_max_attempts", 2)
    for _ in range(2):
        cache_store.register_login_failure("alice|1.1.1.1")

    assert cache_store.login_locked_for("alice|1.1.1.1") > 0
    assert cache_store.login_locked_for("bob|1.1.1.1") == 0, "同 IP 的其他账号不该被锁"
    assert cache_store.login_locked_for("alice|2.2.2.2") == 0, "其他 IP 不该被锁"


# --------------------------------------------------------------------------- 向量缓存


def test_embedding_cache_roundtrip():
    vector = [0.1, 0.2, 0.3] * 50
    cache_store.cache_embedding("ollama:m:150", "火焰系克制什么", vector)
    got = cache_store.get_cached_embedding("ollama:m:150", "火焰系克制什么")
    assert got is not None
    # float32 往返有极小误差，用近似比较
    assert len(got) == len(vector)
    assert got[0] == pytest.approx(0.1, abs=1e-6)


def test_embedding_cache_is_keyed_by_model():
    """★ 缓存键必须包含模型。

    只用文本做键的话，切换 ``EMBEDDING_MODEL`` 后会命中**另一个模型**
    算出的向量。维度可能不同（检索直接崩，或更糟：静默返回错结果），
    而且不报错——表现为"换模型后检索变差了"。
    """
    cache_store.cache_embedding("ollama:modelA:8", "同一段文本", [1.0] * 8)

    assert cache_store.get_cached_embedding("ollama:modelA:8", "同一段文本") is not None
    assert cache_store.get_cached_embedding("ollama:modelB:8", "同一段文本") is None
    assert cache_store.get_cached_embedding("openai:modelA:8", "同一段文本") is None


def test_embedding_cache_is_keyed_by_text():
    cache_store.cache_embedding("m", "问题一", [1.0, 2.0])
    assert cache_store.get_cached_embedding("m", "问题二") is None


def test_embedding_cache_survives_local_reset():
    """跨进程共享：清掉本地缓存后仍能从 Redis 命中。"""
    cache_store.cache_embedding("m", "共享问题", [3.0, 4.0])
    cache_store.reset_local_state()
    assert cache_store.get_cached_embedding("m", "共享问题") is not None


def test_embedding_cache_rejects_empty_input():
    cache_store.cache_embedding("m", "", [1.0])
    assert cache_store.get_cached_embedding("m", "") is None
    cache_store.cache_embedding("m", "text", [])
    assert cache_store.get_cached_embedding("m", "text") is None


# --------------------------------------------------------------------------- 降级


def test_degrades_when_redis_unreachable(monkeypatch):
    """Redis 连不上时必须降级到进程内实现，**功能不缺失**。"""
    from app.config import settings

    cache_client.set_client_for_testing(None)
    cache_client.reset_client()
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6399/0")
    monkeypatch.setattr(settings, "redis_socket_timeout", 0.3)

    ok, detail = cache_client.health()
    assert ok is False
    assert "降级" in detail

    # 三块功能都还能用
    cache_store.revoke_token("local", 60)
    assert cache_store.is_token_revoked("local") is True

    for _ in range(3):
        cache_store.register_login_failure("u|ip")
    assert cache_store.login_failure_count("u|ip") == 3

    cache_store.cache_embedding("m", "t", [1.0, 2.0])
    assert cache_store.get_cached_embedding("m", "t") is not None


def test_unreachable_redis_does_not_stall_every_call(monkeypatch):
    """★ 熔断：Redis 挂掉后不能**每个请求**都等一次连接超时。

    没有熔断时，每次操作都要等 ``REDIS_SOCKET_TIMEOUT``（默认 1 秒），
    比不用缓存还慢一个数量级，线程池会被拖垮。所以失败后进入冷却期，
    冷却期内直接判定不可用。
    """
    from app.config import settings

    cache_client.set_client_for_testing(None)
    cache_client.reset_client()
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:6399/0")
    monkeypatch.setattr(settings, "redis_socket_timeout", 0.5)

    # 第一次触发连接失败并进入冷却期
    cache_client.health()

    started = time.monotonic()
    for _ in range(50):
        cache_store.is_token_revoked("x")
        cache_store.get_cached_embedding("m", "t")
    elapsed = time.monotonic() - started

    # 50 轮 × 2 次操作。有熔断时应该是毫秒级；没有的话会接近 50 秒。
    assert elapsed < 2.0, f"降级路径仍在等待连接（{elapsed:.1f}s），熔断没生效"


def test_unconfigured_redis_is_healthy_not_broken():
    """没配 REDIS_URL 是**正常状态**（单进程部署足够），不是故障。

    健康检查若把它标成失败，界面上会一直挂着一条红色告警，
    用户会以为自己配错了。
    """
    from app.config import settings

    cache_client.set_client_for_testing(None)
    cache_client.reset_client()
    original = settings.redis_url
    try:
        settings.redis_url = ""
        ok, detail = cache_client.health()
        assert ok is True
        assert "进程内" in detail
    finally:
        settings.redis_url = original


def test_redacts_password_in_logs():
    """日志里不能出现 Redis 口令（日志常被收集、转发、长期保留）。"""
    redacted = cache_client._redact("redis://user:s3cret@localhost:6379/0")
    assert "s3cret" not in redacted
    assert "localhost" in redacted
    # 没有口令的 URL 原样返回
    assert cache_client._redact("redis://localhost:6379/0") == "redis://localhost:6379/0"


# --------------------------------------------------------------------------- 检索结果缓存


def _hits(doc: str) -> list[dict]:
    return [{"document_id": doc, "chunk_index": 0, "content": "内容", "score": 0.9}]


def test_search_cache_roundtrip():
    cache_store.cache_search("火焰系", "全部", "alice", False, 4, _hits("d1"))
    got = cache_store.get_cached_search("火焰系", "全部", "alice", False, 4)
    assert got is not None
    assert got[0]["document_id"] == "d1"


def test_search_cache_miss_for_other_query():
    cache_store.cache_search("火焰系", "全部", "alice", False, 4, _hits("d1"))
    assert cache_store.get_cached_search("水系", "全部", "alice", False, 4) is None


def test_search_cache_miss_for_other_category():
    cache_store.cache_search("火焰系", "精灵图鉴", "alice", False, 4, _hits("d1"))
    assert cache_store.get_cached_search("火焰系", "全部", "alice", False, 4) is None


def test_search_cache_miss_for_other_top_k():
    cache_store.cache_search("火焰系", "全部", "alice", False, 4, _hits("d1"))
    assert cache_store.get_cached_search("火焰系", "全部", "alice", False, 8) is None


def test_search_cache_isolates_users():
    """★ 不同用户的检索结果**必须**互不可见。

    检索结果是按可见性过滤过的：alice 的结果里可能有她的私有文档。
    若缓存键只含查询文本，bob 用同样的词就会命中 alice 的结果——
    直接把别人的私有材料交给他。这类漏洞不报错、不 500，
    只是"结果里多了些不该看到的材料"，所以必须在键的设计上堵死。
    """
    cache_store.cache_search("同一问题", "全部", "alice", False, 4, _hits("alice-priv"))
    assert (
        cache_store.get_cached_search("同一问题", "全部", "bob", False, 4) is None
    ), "bob 命中了 alice 的检索结果"


def test_search_cache_isolates_root_from_normal_user():
    """★ root 的全量结果绝不能被普通用户命中（最危险的一种）。

    root 检索时 ``include_all=True``，能看到**所有人**的私有文档。
    若它与普通用户的缓存键相同，普通用户一问就能拿到全部私有材料。
    """
    cache_store.cache_search("同一问题", "全部", "root", True, 4, _hits("bob-priv"))

    # 普通用户（同名但 include_all=False）不能命中
    assert cache_store.get_cached_search("同一问题", "全部", "root", False, 4) is None
    # 其他用户更不能命中
    assert cache_store.get_cached_search("同一问题", "全部", "alice", False, 4) is None
    # root 自己仍能命中
    assert cache_store.get_cached_search("同一问题", "全部", "root", True, 4) is not None


def test_search_cache_key_hides_username():
    """键里不该出现明文用户名（运维能看到 Redis 里有哪些用户）。"""
    key = cache_store._search_key("q", "全部", "alice", False, 4, "1")
    assert "alice" not in key


def test_search_cache_caches_empty_results():
    """空结果也要缓存。

    ``count_searchable`` 预检说"有资料"、但检索被分类过滤成空，是真实会发生的。
    不缓存空结果的话，这种查询每次都白扫一遍全部向量。
    """
    cache_store.cache_search("没有结果的词", "全部", "alice", False, 4, [])
    assert cache_store.get_cached_search("没有结果的词", "全部", "alice", False, 4) == []


def test_corpus_version_bump_invalidates_search_cache():
    """★ 语料变更必须让检索缓存立即失效。

    只用「查询文本」做键的话，用户上传新文档后，同一个问题在 TTL 内
    仍然命中旧结果——表现为"上传成功但搜不到新内容"，而且**不报错**。
    """
    cache_store.cache_search("问题", "全部", "alice", False, 4, _hits("old"))
    assert cache_store.get_cached_search("问题", "全部", "alice", False, 4) is not None

    cache_store.bump_corpus_version()

    assert (
        cache_store.get_cached_search("问题", "全部", "alice", False, 4) is None
    ), "语料版本变了但缓存仍命中（会读到旧结果）"


def test_corpus_version_is_monotonic():
    """版本号必须单调递增。

    用时间戳的话，机器时钟回拨会让版本号"回到过去"，旧缓存重新生效。
    """
    first = int(cache_store.bump_corpus_version())
    second = int(cache_store.bump_corpus_version())
    assert second > first


def test_corpus_version_survives_local_reset():
    """跨进程共享：清掉本地缓存后版本号仍要一致。

    多实例部署时，A 实例上传文档 bump 了版本，B 实例必须也看到新版本，
    否则 B 的旧缓存继续生效。
    """
    version = cache_store.bump_corpus_version()
    cache_store.reset_local_state()
    assert cache_store.corpus_version() == version

