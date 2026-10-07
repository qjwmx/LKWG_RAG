"""可观测性查询层。

只读。写入由 ``observability.writer`` 的后台线程负责。

**百分位在 Python 里算**：SQLite 没有 ``percentile_cont``（实测报错
``no such function``），而项目要同时支持 PostgreSQL。与其写两套 SQL，
不如取有界样本（最近 N 条）在应用层排序——样本量几百条时开销可忽略，
而且两种数据库行为完全一致。

**样本有界**是有意的：P95 在全量数据上算当然更准，但那意味着每次打开
页面都全表扫描。取最近 500 条既能反映"当前"的性能（比历史平均更有用），
又不随数据量增长而变慢。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.models import LlmCallRecord, RequestTrace
from app.db.session import transaction

logger = logging.getLogger(__name__)

# 计算百分位时取多少条最近样本。太大则页面变慢，太小则数字抖动。
PERCENTILE_SAMPLE = 500


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """线性插值百分位。空列表返回 0。

    用插值而不是"取第 k 个"：样本少时（例如只有 5 个请求）取第 k 个会让
    P95 直接等于最大值，看起来像"总是最慢的那次"，误导性很强。
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = fraction * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return float(sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight)


def _window_start(hours: int) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=max(1, hours))


def overview(hours: int = 24) -> dict:
    """总览：请求量、错误率、耗时分布、token 合计、写入器状态。"""
    start = _window_start(hours)

    with transaction() as session:
        total = (
            session.execute(
                select(func.count())
                .select_from(RequestTrace)
                .where(RequestTrace.created_at >= start)
            ).scalar()
            or 0
        )
        errors = (
            session.execute(
                select(func.count())
                .select_from(RequestTrace)
                .where(RequestTrace.created_at >= start, RequestTrace.status >= 400)
            ).scalar()
            or 0
        )
        tokens = (
            session.execute(
                select(func.coalesce(func.sum(RequestTrace.total_tokens), 0)).where(
                    RequestTrace.created_at >= start
                )
            ).scalar()
            or 0
        )
        llm_calls = (
            session.execute(
                select(func.coalesce(func.sum(RequestTrace.llm_calls), 0)).where(
                    RequestTrace.created_at >= start
                )
            ).scalar()
            or 0
        )
        # 有界样本算百分位
        durations = [
            float(row)
            for row in session.execute(
                select(RequestTrace.duration_ms)
                .where(RequestTrace.created_at >= start)
                .order_by(RequestTrace.insert_seq.desc())
                .limit(PERCENTILE_SAMPLE)
            ).scalars()
        ]
        # 缺失用量的调用数：**要显式暴露**，否则"0 token"会被误读成"没花钱"
        missing_usage = (
            session.execute(
                select(func.count())
                .select_from(LlmCallRecord)
                .where(
                    LlmCallRecord.created_at >= start, LlmCallRecord.usage_missing.is_(True)
                )
            ).scalar()
            or 0
        )

    ordered = sorted(durations)
    window_seconds = max(1, hours * 3600)

    return {
        "window_hours": hours,
        "requests": int(total),
        "errors": int(errors),
        "error_rate": round(errors / total, 4) if total else 0.0,
        "qpm": round(total / (window_seconds / 60), 2),
        "total_tokens": int(tokens),
        "llm_calls": int(llm_calls),
        "avg_tokens_per_request": round(tokens / total, 1) if total else 0.0,
        "latency": {
            "avg_ms": round(sum(ordered) / len(ordered), 1) if ordered else 0.0,
            "p50_ms": round(_percentile(ordered, 0.50), 1),
            "p95_ms": round(_percentile(ordered, 0.95), 1),
            "p99_ms": round(_percentile(ordered, 0.99), 1),
            "max_ms": round(ordered[-1], 1) if ordered else 0.0,
            "sample_size": len(ordered),
        },
        "usage_missing_calls": int(missing_usage),
    }


def list_requests(
    limit: int = 50,
    offset: int = 0,
    route: str = "",
    status: str = "",
    username: str = "",
) -> dict:
    """请求轨迹列表。

    ``status`` 支持 ``2xx`` / ``4xx`` / ``5xx`` 这种粗粒度过滤，
    也支持精确数字。
    """
    stmt = select(RequestTrace)
    count_stmt = select(func.count()).select_from(RequestTrace)

    if route:
        stmt = stmt.where(RequestTrace.route.like(f"%{route}%"))
        count_stmt = count_stmt.where(RequestTrace.route.like(f"%{route}%"))
    if username:
        stmt = stmt.where(RequestTrace.username == username)
        count_stmt = count_stmt.where(RequestTrace.username == username)
    if status:
        if status.endswith("xx") and status[0].isdigit():
            low = int(status[0]) * 100
            stmt = stmt.where(RequestTrace.status >= low, RequestTrace.status < low + 100)
            count_stmt = count_stmt.where(
                RequestTrace.status >= low, RequestTrace.status < low + 100
            )
        elif status.isdigit():
            stmt = stmt.where(RequestTrace.status == int(status))
            count_stmt = count_stmt.where(RequestTrace.status == int(status))

    stmt = stmt.order_by(RequestTrace.insert_seq.desc()).limit(max(1, min(limit, 200))).offset(
        max(0, offset)
    )

    with transaction() as session:
        rows = list(session.execute(stmt).scalars())
        total = int(session.execute(count_stmt).scalar() or 0)

    return {
        "requests": [
            {
                "id": r.id,
                "request_id": r.request_id,
                "route": r.route,
                "method": r.method,
                "status": r.status,
                "duration_ms": round(r.duration_ms, 2),
                "username": r.username,
                "error": r.error,
                "llm_calls": r.llm_calls,
                "total_tokens": r.total_tokens,
                "created_at": r.created_at.isoformat(sep=" ") if r.created_at else "",
            }
            for r in rows
        ],
        "total": total,
    }


def llm_breakdown(hours: int = 24) -> dict:
    """按 Agent 分组的调用数、耗时与 token。

    这是本项目最实际的成本问题："五个 Agent 里哪个最贵"。
    """
    start = _window_start(hours)
    with transaction() as session:
        rows = session.execute(
            select(
                LlmCallRecord.agent,
                func.count().label("calls"),
                func.coalesce(func.sum(LlmCallRecord.total_tokens), 0).label("tokens"),
                func.coalesce(func.sum(LlmCallRecord.input_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(LlmCallRecord.output_tokens), 0).label("output_tokens"),
                func.coalesce(func.avg(LlmCallRecord.duration_ms), 0).label("avg_ms"),
                func.coalesce(func.max(LlmCallRecord.duration_ms), 0).label("max_ms"),
            )
            .where(LlmCallRecord.created_at >= start)
            .group_by(LlmCallRecord.agent)
            .order_by(func.sum(LlmCallRecord.total_tokens).desc())
        ).all()

        total_tokens = (
            session.execute(
                select(func.coalesce(func.sum(LlmCallRecord.total_tokens), 0)).where(
                    LlmCallRecord.created_at >= start
                )
            ).scalar()
            or 0
        )

    agents = [
        {
            "agent": row.agent or "unknown",
            "calls": int(row.calls),
            "total_tokens": int(row.tokens),
            "input_tokens": int(row.input_tokens),
            "output_tokens": int(row.output_tokens),
            "avg_duration_ms": round(float(row.avg_ms), 1),
            "max_duration_ms": round(float(row.max_ms), 1),
            "token_share": round(row.tokens / total_tokens, 4) if total_tokens else 0.0,
        }
        for row in rows
    ]
    return {"window_hours": hours, "agents": agents, "total_tokens": int(total_tokens)}


def slowest_routes(hours: int = 24, limit: int = 10) -> list[dict]:
    """最慢的路由（按平均耗时降序）。

    只统计样本数 >= 3 的路由：一次偶发的慢请求会把平均拉到很高，
    那样的"最慢"没有参考价值。
    """
    start = _window_start(hours)
    with transaction() as session:
        rows = session.execute(
            select(
                RequestTrace.route,
                func.count().label("calls"),
                func.coalesce(func.avg(RequestTrace.duration_ms), 0).label("avg_ms"),
                func.coalesce(func.max(RequestTrace.duration_ms), 0).label("max_ms"),
            )
            .where(RequestTrace.created_at >= start)
            .group_by(RequestTrace.route)
            .having(func.count() >= 3)
            .order_by(func.avg(RequestTrace.duration_ms).desc())
            .limit(max(1, min(limit, 50)))
        ).all()

    return [
        {
            "route": row.route,
            "calls": int(row.calls),
            "avg_ms": round(float(row.avg_ms), 1),
            "max_ms": round(float(row.max_ms), 1),
        }
        for row in rows
    ]
