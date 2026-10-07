"""攻略模式的 LangGraph Checkpointer（带 thread_id 的多轮上下文）。

为什么攻略模式需要它，而对话模式不需要
--------------------------------------
对话模式已经**从数据库重建历史**：``rag_graph.retrieve`` 每轮都按
``session_id`` 从 ``qa_logs`` 取最近 N 轮，喂给 ``MessagesPlaceholder("history")``。
它天然就是多轮的。

攻略模式原先完全**无状态**——每次 ``/strategy/research`` 都是全新一轮，
用户追问「那换成水系呢」时，Planner 看到的是一个没有主语的句子，
只能瞎猜。加 checkpointer 之后，同一 ``thread_id`` 的后续轮次能读到上一轮
的研究结果与报告，追问才成立。

**实测过给对话模式也加，结论是不加**：对话模式的 ``messages`` 已经在
``qa_logs`` 里存了一份，checkpoint 里再存一份，两套历史在每轮都被叠加，
第 8 轮消息数到了 56 条（预期 14 条）。所以本模块只服务攻略图。

三个必须做对的地方
------------------
1. **``operator.add`` 归约字段跨轮累积**。``findings`` / ``citations`` 用
   ``operator.add`` 是为了并行 Researcher 扇出（必须），但副作用是
   **它们会跨轮累积**：三轮之后 ``findings`` 里有 T1+T2+T3 的全部条目，
   Analyst 会把上一轮的研究发现当成这一轮的证据。而且**输入传 ``[]``
   不能重置**（``[] + 旧值 = 旧值``）。所以改用 sentinel reducer：
   每轮输入显式传 ``_RESET``，reducer 见到它就清空。
   （实测：不带 sentinel 时第 2 轮 ``findings`` 变成两条。）

2. **历史必须有界**。``turn_history`` 用 ``_bounded_add`` 只留最近几轮，
   否则上下文和 checkpoint 体积都无上限增长。

3. **``thread_id`` 必须按用户隔离**。它是**客户端传来的**，如果直接当
   thread_id 用，用户 A 传 B 的 session_id 就能读到 B 的追问上下文——
   这是跨用户信息泄露。所以真实 thread_id 一律是 ``"{username}:{session_id}"``。

没有 SQLite 时怎么办
--------------------
PostgreSQL 部署下没有 ``SqliteSaver``（它绑 sqlite3）。此时退化成
``InMemorySaver``：**功能不缺失**（同一进程内多轮追问照常），
只是重启后线程记忆丢失。记一条 warning，不静默降级。
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

# 每轮输入用来清空累积字段的哨兵值。
#
# 为什么不能直接用 ``[]``：``operator.add`` 的 reducer 是
# ``cur + new``，传 ``[]`` 得到 ``旧值 + [] = 旧值``——**看起来重置了，
# 其实一个字节都没清**。这个 bug 不会报错，只会让 Analyst 拿到
# 上一轮的研究发现，表现为"模型开始张冠李戴"。
RESET = "__reset__"

# 保留的追问轮数。3 轮足够支撑"上一轮说了什么"这类指代，
# 再多只是白占上下文。
MAX_TURN_HISTORY = 3

def _resolve_db_path() -> str | None:
    """决定 checkpoint 库落在哪。返回 None 表示**不该用 SQLite saver**。

    优先用 ``CHECKPOINT_DB`` 显式配置（测试指向临时目录就靠它）；
    留空时与业务库同目录，**文件名固定**（不像业务库那样可被 DATABASE_URL 改掉）——
    checkpoint 是旁路数据，不该因为换了业务库就散落到别处。

    ``CHECKPOINT_DB`` 显式配置时**优先于业务库类型**：部署在 PostgreSQL 上
    但想把 checkpoint 放本地 SQLite 文件，这是合理配置。
    """
    configured = (settings.checkpoint_db or "").strip()
    if configured:
        path = Path(configured)
        path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    # 业务库若是 SQLite，就放在它旁边（同目录不同文件）；
    # 不是 SQLite（PostgreSQL 等）则返回 None，由调用方退化为 InMemorySaver。
    business = _sqlite_path_from_url(settings.database_url)
    if business is None:
        return None
    sibling = Path(business).parent / "checkpoints.db"
    sibling.parent.mkdir(parents=True, exist_ok=True)
    return str(sibling)

# thread_id 白名单。参数化查询本身防注入，这里限制字符集是为了
# 防止有人拿超长字符串或奇怪字符把 checkpoint 表搞脏。
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,96}$")


def reset_add(current: Any, new: Any) -> list:
    """sentinel-aware 的 ``operator.add``。

    见到 ``RESET`` 就清空——这是每轮开始重置累积字段的唯一手段（见模块 docstring）。
    """
    if new is RESET or new == RESET:
        return []
    if not isinstance(current, list):
        current = []
    if new is None:
        return current
    if not isinstance(new, list):
        new = [new]
    return current + new


def bounded_add(limit: int):
    """有界累积：只保留最后 ``limit`` 条。

    用于 ``turn_history``——历史不能无上限增长，否则跑几十轮之后
    上下文长度和 checkpoint 体积都会失控。
    """

    def reducer(current: Any, new: Any) -> list:
        if new is RESET or new == RESET:
            return []
        if not isinstance(current, list):
            current = []
        if new is None:
            return current
        if not isinstance(new, list):
            new = [new]
        combined = current + new
        return combined[-limit:] if limit > 0 else []

    return reducer


def normalize_session_id(raw: str) -> str:
    """校验并归一化客户端传来的 session_id。不合法就生成一个新的。

    **不抛异常**：session_id 是前端生成的辅助标识，格式不对不该让
    整个请求失败——生成一个临时 id 继续跑（只是那一轮不共享上下文）。
    """
    import secrets

    candidate = (raw or "").strip()
    if candidate and _THREAD_ID_RE.match(candidate):
        return candidate
    return f"strat_{secrets.token_hex(8)}"


def thread_id_for(username: str, session_id: str) -> str:
    """真实的 checkpoint thread_id。

    **必须带用户名前缀**：session_id 来自客户端，不隔离的话
    用户 A 传 B 的 session_id 就能读到 B 的追问上下文。
    """
    safe_user = (username or "anonymous").strip() or "anonymous"
    # 用户名也可能含冒号之类，替换掉以免前缀被伪造
    safe_user = re.sub(r"[^A-Za-z0-9_.\-]", "_", safe_user)[:48]
    return f"{safe_user}:{normalize_session_id(session_id)}"


# --------------------------------------------------------------------------- saver 单例

_saver: Any = None
_saver_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _sqlite_path_from_url(url: str) -> str | None:
    """从 SQLAlchemy URL 里取出 SQLite 文件路径。非 SQLite 返回 None。"""
    if not url or not url.startswith("sqlite"):
        return None
    # sqlite:///abs/path  -> abs/path
    # sqlite:////abs/path -> /abs/path
    _, _, tail = url.partition(":///")
    if not tail:
        return None
    if tail.startswith("/") and not re.match(r"^[A-Za-z]:", tail):
        # 形如 sqlite:////tmp/x.db 的绝对路径（POSIX）
        return "/" + tail.lstrip("/")
    if tail in (":memory:", ""):
        return None
    return tail


def get_saver():
    """取（并缓存）checkpointer。

    第一次调用时建表并做一次线程裁剪。整个进程共用一个——
    ``SqliteSaver`` 内部有锁，``check_same_thread=False`` 下是线程安全的。
    """
    global _saver, _conn
    if not settings.checkpoint_enabled:
        return None
    if _saver is not None:
        return _saver

    with _saver_lock:
        if _saver is not None:
            return _saver

        path = _resolve_db_path()
        if path is None:
            from langgraph.checkpoint.memory import InMemorySaver

            # 明确记日志：不静默降级。PostgreSQL 部署下重启会丢线程记忆，
            # 这是运维需要知道的行为差异。
            logger.warning(
                "非 SQLite 数据库，checkpointer 退化为 InMemorySaver："
                "多轮追问在同一进程内有效，重启后丢失。"
            )
            _saver = InMemorySaver()
            return _saver

        try:
            _conn = sqlite3.connect(path, check_same_thread=False)
            # temp_store=MEMORY：与 app/db/session.py 同理。
            # 大语句落盘时会去写系统临时目录，而某些环境下那是不可写的，
            # 报出来是极具误导性的 "unable to open database file"。
            _conn.execute("PRAGMA temp_store=MEMORY")
            _conn.execute("PRAGMA busy_timeout=5000")

            from langgraph.checkpoint.sqlite import SqliteSaver

            saver = SqliteSaver(_conn)
            saver.setup()
        except Exception:  # noqa: BLE001 - checkpoint 不该阻断服务启动
            logger.exception("初始化 SqliteSaver 失败，退化为 InMemorySaver")
            from langgraph.checkpoint.memory import InMemorySaver

            _saver = InMemorySaver()
            return _saver

        _saver = saver
        logger.info("checkpointer 就绪：%s", path)

    # 裁剪放在锁外：它只是维护操作，失败也不该影响可用性
    try:
        prune_threads(settings.checkpoint_max_threads)
    except Exception:  # noqa: BLE001
        logger.warning("checkpoint 线程裁剪失败（不影响功能）", exc_info=True)

    return _saver


def reset_saver() -> None:
    """测试用：丢掉缓存的 saver。"""
    global _saver, _conn
    with _saver_lock:
        _saver = None
        if _conn is not None:
            try:
                _conn.close()
            except Exception:  # noqa: BLE001
                pass
        _conn = None


def _maintenance_lock():
    """维护操作要用的锁。

    **必须复用 saver 自己的锁**（``SqliteSaver.lock``）。它把每个
    checkpoint 读写都包在 ``with self.lock`` 里，而裁剪/统计直接操作同一个
    ``sqlite3.Connection``——不走同一把锁的话，两个线程可能同时在**同一个
    连接**上开事务（``BEGIN`` 嵌套会报 "cannot start a transaction within
    a transaction"，或者更糟：一个线程的 ROLLBACK 把另一个线程的写入吞掉）。
    单连接 + 多线程本来就必须串行化，这里只是把既有的锁用起来。
    """
    saver = _saver
    lock = getattr(saver, "lock", None)
    if lock is not None:
        return lock
    # 没有 saver（InMemorySaver 或未初始化）时用自己的锁兜底
    return _saver_lock


def prune_threads(keep: int) -> int:
    """只保留最近 ``keep`` 个线程，返回删掉的线程数。

    checkpoint 表只增不减，长期运行会慢慢撑大 SQLite。按**最后活动时间**
    排序（checkpoint_id 是单调递增的 ULID，字典序即时间序）。
    """
    if keep <= 0 or _conn is None:
        return 0

    with _maintenance_lock():
        rows = _conn.execute(
            "SELECT thread_id, MAX(checkpoint_id) AS last FROM checkpoints "
            "GROUP BY thread_id ORDER BY last DESC"
        ).fetchall()
        if len(rows) <= keep:
            return 0

        doomed = [row[0] for row in rows[keep:]]
        # 一次事务删完：逐条 delete 会各开一个隐式事务，几百个线程会明显变慢
        _conn.execute("BEGIN")
        try:
            for thread_id in doomed:
                _conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
                _conn.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
            _conn.execute("COMMIT")
        except Exception:
            _conn.execute("ROLLBACK")
            raise

    logger.info("checkpoint 裁剪：删除 %d 个旧线程（保留 %d）", len(doomed), keep)
    return len(doomed)


def thread_stats() -> dict[str, int]:
    """给 /strategy/web_status 用的维护信息。"""
    if _conn is None:
        return {"threads": 0, "checkpoints": 0}
    try:
        with _maintenance_lock():
            threads = _conn.execute(
                "SELECT COUNT(DISTINCT thread_id) FROM checkpoints"
            ).fetchone()[0]
            total = _conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    except Exception:  # noqa: BLE001
        return {"threads": 0, "checkpoints": 0}
    return {"threads": int(threads or 0), "checkpoints": int(total or 0)}


def delete_thread(username: str, session_id: str) -> None:
    """删掉一个线程（用户点「新对话」时可以顺手清理）。"""
    saver = get_saver()
    if saver is None:
        return
    try:
        saver.delete_thread(thread_id_for(username, session_id))
    except Exception:  # noqa: BLE001
        logger.warning("删除 checkpoint 线程失败", exc_info=True)


def has_history(username: str, session_id: str) -> bool:
    """这个线程是否已经有历史（前端用来决定要不要显示"继续上次追问"）。"""
    saver = get_saver()
    if saver is None:
        return False
    try:
        return saver.get_tuple({"configurable": {"thread_id": thread_id_for(username, session_id)}}) is not None
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "MAX_TURN_HISTORY",
    "RESET",
    "bounded_add",
    "delete_thread",
    "get_saver",
    "has_history",
    "normalize_session_id",
    "prune_threads",
    "reset_add",
    "reset_saver",
    "thread_id_for",
    "thread_stats",
]
