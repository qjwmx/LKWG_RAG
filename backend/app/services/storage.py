"""查询层：文档、问答日志、会话、反馈、统计。

对外返回**普通 dict**而不是 ORM 实体或 pydantic 模型——调用方一律按
``record["id"]`` 这样取值，任何"更优雅"的返回类型都会让序列化多一层转换。

**可见范围统一由 ``include_all`` 驱动**：
- 普通用户（``include_all=False``）：公共库 + 自己上传的私有文档。
- root（``include_all=True``）：**全部**，含所有人的私有文档。

过滤一律下推到 SQL。在应用层"取回来再筛"的话，任何忘记再筛一次的调用点
都会漏——可见性规则必须在数据源头生效。
"""

from __future__ import annotations

import datetime as dt
import logging
from uuid import uuid4

from sqlalchemy import and_, delete, func, or_, select, update

from app.cache import bump_corpus_version
from app.config import SCOPE_PRIVATE, SCOPE_PUBLIC
from app.db.models import DocumentRecord, FeedbackLog, QaLog
from app.db.session import transaction

logger = logging.getLogger(__name__)


def _to_dict(mapping) -> dict:
    """ORM 行 -> 普通 dict。

    剔除 ``insert_seq``：它是内部排序键，出现在返回里会让前端多一个无意义字段。
    datetime 统一格式化成字符串，避免时区在序列化层被悄悄改掉。
    """
    result = {}
    for key, value in mapping.items():
        if key == "insert_seq":
            continue
        result[key] = _fmt(value) if isinstance(value, dt.datetime) else value
    return result


def _orm_to_dict(instance) -> dict:
    return _to_dict({c.name: getattr(instance, c.name) for c in instance.__table__.columns})


def _fmt(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    # SQLite 存 naive UTC；带 tzinfo 的（PostgreSQL）先转 UTC 再去掉，
    # 否则界面时间会整体偏 8 小时而且不报错。
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _next_seq(session, model) -> int:
    """取下一个 ``insert_seq``。

    **必须在调用方的事务里执行**：``MAX + 1`` 不是原子操作，两个并发事务可能
    取到同一个号。不过 ``insert_seq`` 有唯一约束，冲突时后提交的那个会失败并
    回滚，不会静默写出错误顺序——对这个体量的项目，这个取舍比引入一个独立
    序列表更划算。

    为什么不用数据库自增：``Identity()`` 在 SQLite 上不生成值（直接撞 NOT NULL），
    ``BIGSERIAL`` 只存在于 PostgreSQL。这里统一在应用层取号，两种库行为一致。
    """
    current = session.execute(select(func.max(model.insert_seq))).scalar()
    return int(current or 0) + 1


# --------------------------------------------------------------------------- 文档


def list_documents(
    requester: str = "", include_all: bool = False, category: str | None = None
) -> list[dict]:
    stmt = select(DocumentRecord).order_by(
        DocumentRecord.uploaded_at.desc(), DocumentRecord.insert_seq.asc()
    )
    if not include_all:
        stmt = stmt.where(
            or_(
                DocumentRecord.scope == SCOPE_PUBLIC,
                and_(
                    DocumentRecord.scope == SCOPE_PRIVATE,
                    DocumentRecord.owner == (requester or ""),
                ),
            )
        )
    if category:
        stmt = stmt.where(DocumentRecord.category == category)
    with transaction() as session:
        rows = session.execute(stmt).scalars().all()
    return [_orm_to_dict(r) for r in rows]


def get_document(document_id: str) -> dict | None:
    with transaction() as session:
        row = session.execute(
            select(DocumentRecord).where(DocumentRecord.id == document_id)
        ).scalars().first()
    return _orm_to_dict(row) if row else None


def get_document_by_hash(file_hash: str) -> dict | None:
    with transaction() as session:
        row = session.execute(
            select(DocumentRecord).where(DocumentRecord.file_hash == file_hash)
        ).scalars().first()
    return _orm_to_dict(row) if row else None


def add_document(record: dict) -> dict:
    values = dict(record)
    values.setdefault("id", uuid4().hex)
    values.pop("insert_seq", None)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    values["updated_at"] = now
    values.setdefault("uploaded_at", now)

    with transaction() as session:
        values["insert_seq"] = _next_seq(session, DocumentRecord)
        row = DocumentRecord(**values)
        session.add(row)
        session.flush()
        return _orm_to_dict(row)


def update_document(document_id: str, patch: dict) -> dict | None:
    values = {k: v for k, v in patch.items() if k not in ("id", "insert_seq", "file_hash")}
    if not values:
        return get_document(document_id)
    values["updated_at"] = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    with transaction() as session:
        session.execute(
            update(DocumentRecord).where(DocumentRecord.id == document_id).values(**values)
        )
    # ★ ``status`` 决定文档能否被检索（检索侧 ``WHERE status='success'``）。
    # 状态从 processing → success 就是"这份材料刚变得可搜"，缓存必须失效，
    # 否则上传完成后 TTL 内仍然搜不到——表现为"上传成功但检索不到新内容"。
    if "status" in values:
        bump_corpus_version()
    return get_document(document_id)


def delete_document(document_id: str) -> bool:
    """删文档记录。

    **副作用是核心收益**：``document_chunks.document_id`` 是 ON DELETE CASCADE，
    该文档的全部向量在**同一个事务里**一并删除。调用方**刻意不要**再调
    ``vector_store.delete_chunks``——那会变成两套删除路径，反而容易漏。

    （SQLite 需要 ``PRAGMA foreign_keys=ON`` 才真的级联，见 db/session.py。）
    """
    with transaction() as session:
        result = session.execute(
            delete(DocumentRecord).where(DocumentRecord.id == document_id)
        )
        removed = (result.rowcount or 0) > 0
    # ★ 级联删掉了切片，但**没走** vector_store.delete_chunks，
    # 所以那里的失效逻辑覆盖不到这条路径。删文档后必须显式失效，
    # 否则被删的内容还能从缓存里被检索出来——用户删了私有文档却仍能搜到它。
    if removed:
        bump_corpus_version()
    return removed


# --------------------------------------------------------------------------- 问答日志


def add_qa_log(record: dict) -> dict:
    values = dict(record)
    values.setdefault("id", uuid4().hex)
    values.setdefault("source_docs", [])
    # 默认空列表：不开联网时前端不该拿到 None，否则每次都要写判空
    values.setdefault("web_sources", [])
    values.pop("insert_seq", None)
    values.setdefault("created_at", dt.datetime.now(dt.timezone.utc).replace(tzinfo=None))

    with transaction() as session:
        values["insert_seq"] = _next_seq(session, QaLog)
        row = QaLog(**values)
        session.add(row)
        session.flush()
        return _orm_to_dict(row)


def list_session_logs(session_id: str, limit: int | None = None) -> list[dict]:
    """取某会话的最近 limit 轮问答，**按时间升序**返回。

    注意不能写成 ``ORDER BY created_at ASC LIMIT n``——那返回的是**最旧**的 n 条，
    后果是喂给模型错误的对话轮次，表现成"AI 忘了最近说的话"，而且不报错。
    必须用子查询：先降序取 n 条，再整体升序。
    """
    inner = (
        select(QaLog)
        .where(QaLog.session_id == session_id)
        .order_by(QaLog.created_at.desc(), QaLog.insert_seq.asc())
    )
    if limit:
        inner = inner.limit(int(limit))
    subquery = inner.subquery()
    stmt = select(subquery).order_by(
        subquery.c.created_at.asc(), subquery.c.insert_seq.asc()
    )
    with transaction() as session:
        rows = session.execute(stmt).all()
    return [_to_dict(r._mapping) for r in rows]


def list_qa_logs(limit: int | None = None) -> list[dict]:
    stmt = select(QaLog).order_by(QaLog.created_at.desc(), QaLog.insert_seq.asc())
    if limit:
        stmt = stmt.limit(int(limit))
    with transaction() as session:
        rows = session.execute(stmt).scalars().all()
    return [_orm_to_dict(r) for r in rows]


def list_sessions(
    limit: int = 20, offset: int = 0, username: str = "", include_all: bool = False
) -> list[dict]:
    """按 ``session_id`` 聚合出会话列表，最近的在前。

    用窗口函数一次算完，避免"先查会话再逐个查首问"的 N+1；
    ``rn = 1`` 那行就是该会话的第一个问题（拿来当标题）。
    """
    base = select(
        QaLog.session_id.label("session_id"),
        QaLog.question.label("question"),
        QaLog.category.label("category"),
        QaLog.username.label("username"),
        func.row_number()
        .over(partition_by=QaLog.session_id, order_by=QaLog.insert_seq)
        .label("rn"),
        func.count().over(partition_by=QaLog.session_id).label("turns"),
        func.min(QaLog.created_at).over(partition_by=QaLog.session_id).label("started_at"),
        func.max(QaLog.created_at).over(partition_by=QaLog.session_id).label("last_at"),
    ).where(QaLog.session_id != "")

    if not include_all:
        base = base.where(QaLog.username == (username or ""))

    ranked = base.subquery()
    stmt = (
        select(ranked)
        .where(ranked.c.rn == 1)
        .order_by(ranked.c.last_at.desc())
        .limit(int(limit) if limit else 20)
        .offset(int(offset or 0))
    )
    with transaction() as session:
        rows = session.execute(stmt).all()
    # rn 只是筛"每个会话第一行"的内部序号，不往外暴露
    return [{k: v for k, v in _to_dict(r._mapping).items() if k != "rn"} for r in rows]


def get_session_owner(session_id: str) -> str:
    """返回某会话的提问者。取第一条非空 username；全空返回空串。"""
    stmt = (
        select(QaLog.username)
        .where(QaLog.session_id == session_id, QaLog.username != "")
        .limit(1)
    )
    with transaction() as session:
        return session.execute(stmt).scalar_one_or_none() or ""


def delete_session(session_id: str) -> int:
    """删除某会话的全部问答记录（反馈随外键级联删除）。返回删除行数。"""
    with transaction() as session:
        result = session.execute(delete(QaLog).where(QaLog.session_id == session_id))
        return result.rowcount or 0


# --------------------------------------------------------------------------- 反馈


def list_feedback() -> list[dict]:
    stmt = select(FeedbackLog).order_by(FeedbackLog.created_at.desc())
    with transaction() as session:
        rows = session.execute(stmt).scalars().all()
    return [_orm_to_dict(r) for r in rows]


def get_feedback(qa_log_id: str) -> dict | None:
    with transaction() as session:
        row = session.execute(
            select(FeedbackLog).where(FeedbackLog.qa_log_id == qa_log_id)
        ).scalars().first()
    return _orm_to_dict(row) if row else None


def upsert_feedback(qa_log_id: str, rating: str, comment: str, session_id: str) -> dict:
    """按 ``qa_log_id`` upsert。

    更新时**只改** rating / comment / updated_at：
    - created_at 保持首次提交时间；
    - session_id 保持首次提交时的会话（用户可能在另一个会话里回来改评价）。
    """
    existing = get_feedback(qa_log_id)
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)

    if existing:
        with transaction() as session:
            session.execute(
                update(FeedbackLog)
                .where(FeedbackLog.qa_log_id == qa_log_id)
                .values(rating=rating, comment=comment, updated_at=now)
            )
    else:
        with transaction() as session:
            session.add(
                FeedbackLog(
                    id=uuid4().hex,
                    qa_log_id=qa_log_id,
                    rating=rating,
                    comment=comment,
                    session_id=session_id,
                    created_at=now,
                    updated_at=now,
                )
            )
    return get_feedback(qa_log_id) or {}


# --------------------------------------------------------------------------- 统计


def get_stats() -> dict:
    """一条查询取四项统计。"""
    statement = select(
        select(func.count()).select_from(DocumentRecord).scalar_subquery().label("document_count"),
        # 必须排除空分类，否则"覆盖分类"永远比实际多 1
        select(func.count(func.distinct(DocumentRecord.category)))
        .select_from(DocumentRecord)
        .where(DocumentRecord.category != "")
        .scalar_subquery()
        .label("category_count"),
        select(func.count()).select_from(QaLog).scalar_subquery().label("qa_count"),
        select(func.count()).select_from(FeedbackLog).scalar_subquery().label("feedback_count"),
    )
    with transaction() as session:
        row = session.execute(statement).one()
    return dict(row._mapping)
