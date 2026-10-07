"""轨迹写入器：有界队列 + 后台线程批量落库。

为什么不能同步写
----------------
实测 SQLite 默认（DELETE journal）下单条 INSERT + commit 要 **2.05ms**。
这个项目里最快的接口只要 3ms，同步写轨迹会把它拖慢一倍以上——
**观测本身成了性能问题**，这是最不该发生的事。

三个数字说明为什么这么做：
- 同步写：**2.05ms / 请求**（不可接受）
- 入队：**1.2µs / 请求**（实测，便宜 1700 倍）
- 批量落库（WAL + 100 条一批）：**0.022ms / 行**

队列满了怎么办
--------------
**丢弃，并计数。** 观测数据是可丢弃的旁路信息，绝不能反压业务——
一个满队列让用户请求失败，是拿可用性换指标，方向反了。
丢弃数会暴露在总览接口里，所以"丢了"不会被悄悄藏起来。

关闭时的语义
------------
``stop()`` 会等队列排空（带超时），尽量不丢已入队的数据。
但**不保证**：进程被 kill -9 时队列里的数据必然丢。这是可接受的取舍
——轨迹不是账本。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.observability.context import RequestRecord

logger = logging.getLogger(__name__)

# 一批最多写多少行。太小则 commit 次数多（每次 commit 都要 fsync），
# 太大则队列里的数据在崩溃时丢得更多、且延迟可见性变差。
_BATCH_SIZE = 100
# 队列空时的等待时间。决定了"最后一条数据多久落库"——0.2 秒足够，
# 也避免了忙等。
_POLL_TIMEOUT = 0.2
# stop() 等待排空的超时
_DRAIN_TIMEOUT = 3.0
# 每多少次写入触发一次保留策略清理。
# 不能每次都清理（那是一次全表扫描），也不能只靠定时（进程可能短命）。
_CLEANUP_EVERY = 50


class TraceWriter:
    """单例式的后台写入器。"""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue(
            maxsize=max(1, int(settings.observability_queue_size))
        )
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 丢弃计数：队列满时累加。暴露给总览接口，避免"悄悄丢数据"。
        self._dropped = 0
        self._written = 0
        self._cleanup_counter = 0
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        """启动后台线程。幂等——重复调用不会起第二个线程。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="observability-writer", daemon=True
        )
        self._thread.start()
        logger.info("可观测性写入器已启动（队列容量 %d）", self._queue.maxsize)

    def stop(self) -> None:
        """停止并尽量排空。幂等。"""
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=_DRAIN_TIMEOUT)
        self._thread = None
        logger.info(
            "可观测性写入器已停止（累计写入 %d，丢弃 %d）", self._written, self._dropped
        )

    # ---------------------------------------------------------------- 投递

    def submit(self, record: RequestRecord) -> bool:
        """投递一条请求轨迹。**永不阻塞、永不抛异常**。

        返回是否成功入队。失败只累加丢弃计数——观测失败绝不能影响业务。
        """
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            with self._lock:
                self._dropped += 1
            # 只在第一次丢时告警，否则队列持续满会刷爆日志
            if self._dropped == 1:
                logger.warning(
                    "可观测性队列已满，开始丢弃轨迹（不影响业务请求）"
                )
            return False
        except Exception:  # noqa: BLE001 - 兜底：埋点绝不能抛
            return False

    def flush_now(self, timeout: float = 2.0) -> None:
        """阻塞到队列排空。**测试用**。

        测试不能靠 ``sleep`` 等异步写入——那既慢又不稳定。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty():
                # 队列空了不代表正在写的那批已完成，再让出一次
                time.sleep(0.02)
                if self._queue.empty():
                    return
            time.sleep(0.01)

    # ---------------------------------------------------------------- 后台循环

    def _run(self) -> None:
        while True:
            batch = self._collect()
            if batch:
                self._write(batch)
            if self._stop.is_set() and self._queue.empty():
                # 停止信号 + 队列已空 -> 收工。最后再收一批，
                # 避免"停止瞬间正好有新数据入队"而漏掉。
                extra = self._collect()
                if extra:
                    self._write(extra)
                if self._queue.empty():
                    return
            if not batch:
                time.sleep(_POLL_TIMEOUT)

    def _collect(self) -> list[RequestRecord]:
        """尽量凑一批。取不到就返回空（调用方决定要不要等）。"""
        batch: list[RequestRecord] = []
        try:
            batch.append(self._queue.get_nowait())
        except queue.Empty:
            return batch
        while len(batch) < _BATCH_SIZE:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def _write(self, batch: list[RequestRecord]) -> None:
        """批量落库。**任何异常都吞掉并记日志**——写入失败不该让线程死掉，
        更不该影响业务。"""
        try:
            self._persist(batch)
            with self._lock:
                self._written += len(batch)
            self._cleanup_counter += len(batch)
            if self._cleanup_counter >= _CLEANUP_EVERY:
                self._cleanup_counter = 0
                self._cleanup()
        except Exception:  # noqa: BLE001
            logger.exception("可观测性轨迹写入失败（丢弃 %d 条）", len(batch))

    def _persist(self, batch: list[RequestRecord]) -> None:
        """批量落库。

        ``insert_seq`` **每批只取一次基数、然后在本地递增**，不是每行都调
        ``_next_seq``。原因：那个 helper 每次都要发一条 ``SELECT MAX(insert_seq)``，
        一批 100 个请求 × 5 次模型调用 = 600 次查询，实测把 600 行的写入
        从 ~10ms 拖到 ~300ms（30 倍）。

        本地递增是安全的：整批在**同一个事务**里，而且这个写入器是
        **单线程**的（只有一个后台线程调用本方法），所以不存在并发取到
        同一个号的竞态。
        """
        from sqlalchemy import func, select

        from app.db.models import LlmCallRecord, RequestTrace
        from app.db.session import transaction

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with transaction() as session:
            trace_base = int(
                session.execute(select(func.max(RequestTrace.insert_seq))).scalar() or 0
            )
            call_base = int(
                session.execute(select(func.max(LlmCallRecord.insert_seq))).scalar() or 0
            )

            for offset, record in enumerate(batch):
                session.add(
                    RequestTrace(
                        id=record.request_id,
                        insert_seq=trace_base + offset + 1,
                        request_id=record.request_id,
                        route=record.route,
                        method=record.method,
                        status=record.status,
                        duration_ms=record.duration_ms,
                        username=record.username,
                        error=record.error,
                        llm_calls=len(record.llm_calls),
                        total_tokens=record.total_tokens,
                        created_at=now,
                    )
                )
                for index, call in enumerate(record.llm_calls):
                    session.add(
                        LlmCallRecord(
                            # 主键要全局唯一：同一次请求可能调多次模型
                            id=f"{record.request_id}-{index}",
                            insert_seq=call_base + index + 1,
                            request_id=record.request_id,
                            agent=call.agent,
                            model=call.model,
                            input_tokens=call.input_tokens,
                            output_tokens=call.output_tokens,
                            total_tokens=call.total_tokens,
                            duration_ms=call.duration_ms,
                            structured=call.structured,
                            usage_missing=call.usage_missing,
                            created_at=now,
                        )
                    )
                call_base += len(record.llm_calls)

    def _cleanup(self) -> None:
        """保留策略：删超期行 + 硬上限。

        两件事都要做：
        - **超期**：按天数裁剪，防止无限增长
        - **上限**：即使一天内的数据也可能撑爆单机 SQLite（流量突增时
          7 天的量可能远超预期），所以再加一道行数上限
        """
        from sqlalchemy import delete, func, select

        from app.db.models import LlmCallRecord, RequestTrace
        from app.db.session import transaction

        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
            days=max(1, int(settings.observability_retention_days))
        )
        try:
            with transaction() as session:
                session.execute(
                    delete(RequestTrace).where(RequestTrace.created_at < cutoff)
                )
                session.execute(
                    delete(LlmCallRecord).where(LlmCallRecord.created_at < cutoff)
                )

                # 硬上限：超出时删最旧的
                max_rows = max(100, int(settings.observability_max_rows))
                total = (
                    session.execute(
                        select(func.count()).select_from(RequestTrace)
                    ).scalar()
                    or 0
                )
                if total > max_rows:
                    excess = total - max_rows
                    old_ids = list(
                        session.execute(
                            select(RequestTrace.id)
                            .order_by(RequestTrace.insert_seq.asc())
                            .limit(excess)
                        ).scalars()
                    )
                    if old_ids:
                        session.execute(
                            delete(RequestTrace).where(RequestTrace.id.in_(old_ids))
                        )
                        # 用量记录**不跟着删**：它比轨迹更值得保留，
                        # 而且是成本统计的来源（见 LlmCallRecord 的说明）。
        except Exception:  # noqa: BLE001
            logger.exception("可观测性保留策略执行失败（不影响业务）")

    # ---------------------------------------------------------------- 诊断

    def stats(self) -> dict:
        with self._lock:
            return {
                "queue_size": self._queue.qsize(),
                "queue_capacity": self._queue.maxsize,
                "written": self._written,
                "dropped": self._dropped,
                "running": self._thread is not None and self._thread.is_alive(),
            }


_writer: TraceWriter | None = None


def get_writer() -> TraceWriter:
    """全局写入器。惰性创建——导入本模块不该启动线程。"""
    global _writer
    if _writer is None:
        _writer = TraceWriter()
    return _writer


def reset_writer() -> None:
    """测试用：停掉并丢弃当前写入器。"""
    global _writer
    if _writer is not None:
        _writer.stop()
    _writer = None
