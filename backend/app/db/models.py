"""数据库模型。

两个命名注意：
- 文档实体叫 ``DocumentRecord`` 而不是 ``Document``——``langchain``/通用代码里
  ``Document`` 是高频名字，同名会静默遮蔽，报错点会飘到很远的地方。
- 用户实体叫 ``UserRecord``，同理避开 ``User``。

可空性刻意全部用 ``NOT NULL DEFAULT ''`` 而不是 NULL：取出来永远是字符串，
展示层不必到处判 None。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.db.types import VectorBlob


class Base(DeclarativeBase):
    pass


def _now_column():
    return mapped_column(TIMESTAMP, nullable=False, server_default=func.now())


def _seq_column() -> Mapped[int]:
    """稳定排序键。

    **刻意不用 ``Identity()`` / ``AUTOINCREMENT``**：它在 SQLite 上不会自动生成
    （插入时 insert_seq 为 NULL，直接撞 NOT NULL 约束），而 ``BIGSERIAL`` 只存在于
    PostgreSQL。用普通 BIGINT + 唯一约束，由 ``storage`` 在**同一事务内**用
    ``MAX(insert_seq) + 1`` 取号，两种数据库行为一致。

    为什么需要它：时间戳在 SQLite 上只有秒级精度，同一秒内插入的多行排序不确定，
    表现为"刷新页面后列表顺序变了"。有了单调递增的次级排序键，顺序才稳定。
    """
    return mapped_column(BigInteger, unique=True, nullable=False, index=False)


class UserRecord(Base):
    """注册用户。

    **管理员（root）不在这张表里**：它由 .env 预置，不落库，因此注册流程
    既覆盖不了它、也抢注不了。把 root 放进同一张表，"第一个注册的人叫什么"
    就变成一个安全边界问题。

    ``role`` 列仍然存在，是为了支持 root 把某个已通过审批的用户提为管理员
    （受 ``ALLOW_ADMIN_PROMOTION`` 约束）。
    """

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(Text, primary_key=True)
    # pbkdf2_sha256$迭代次数$盐$哈希
    password_hash: Mapped[str] = mapped_column(Text, nullable=False, default="")
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    role: Mapped[str] = mapped_column(Text, nullable=False, default="user")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    created_at: Mapped[dt.datetime] = _now_column()
    updated_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        CheckConstraint("role IN ('admin', 'user')", name="users_role_check"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="users_status_check"
        ),
        Index("users_status_idx", "status"),
    )


class DocumentRecord(Base):
    """文档元数据。"""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # 仅用于排序 tiebreak，不对外暴露（见 storage._to_dict）。
    insert_seq: Mapped[int] = _seq_column()
    # 去重依据（sha256）。钉成唯一约束后，"同一份文档各存一份、两份都命中检索"
    # 在数据库层不可能发生。
    file_hash: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    file_type: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # scope='public'  —— root 上传，所有登录用户可检索
    # scope='private' —— 普通用户上传，只有 owner 本人（以及 root）可检索
    owner: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope: Mapped[str] = mapped_column(Text, nullable=False, default="public")
    raw_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    text_length: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="processing")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    uploaded_at: Mapped[dt.datetime] = _now_column()
    updated_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        UniqueConstraint("file_hash", name="documents_file_hash_key"),
        CheckConstraint(
            "status IN ('processing', 'success', 'failed')", name="documents_status_check"
        ),
        CheckConstraint("chunk_count >= 0", name="documents_chunk_count_check"),
        CheckConstraint("text_length >= 0", name="documents_text_length_check"),
        CheckConstraint("scope IN ('public', 'private')", name="documents_scope_check"),
        Index("documents_uploaded_at_idx", "uploaded_at"),
        Index("documents_category_idx", "category"),
        # 检索与列表的可见性过滤走这条索引
        Index("documents_scope_owner_idx", "scope", "owner"),
    )


class DocumentChunk(Base):
    """文档切片与向量。

    只存 ``document_id`` 与向量本体，``title`` / ``category`` / ``version``
    一概不冗余，检索时 JOIN ``documents`` 取。

    这么选是刻意的：冗余一份元数据副本，就等于把"两套存储各存一份、
    谁也不知道哪份是对的"那个病搬进了新库。JOIN 以主键命中，代价可忽略。
    """

    __tablename__ = "document_chunks"

    document_id: Mapped[str] = mapped_column(
        Text, ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    # 0-based。**引用高亮靠它定位**：前端拿到 source_docs[].chunk_index 后
    # 拉到全部切片并按这个下标高亮。
    chunk_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[bytes] = mapped_column(VectorBlob, nullable=False)
    created_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        CheckConstraint("chunk_index >= 0", name="document_chunks_chunk_index_check"),
        # (document_id, chunk_index) 的主键前导列就是 document_id，
        # 级联删除可以直接走索引，不必全表扫描 chunk 表。
    )


class QaLog(Base):
    """问答日志。同时是对话历史的**唯一真源**（按 session_id 回查重建）。"""

    __tablename__ = "qa_logs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    insert_seq: Mapped[int] = _seq_column()
    session_id: Mapped[str] = mapped_column(Text, nullable=False)
    # 提问者。历史会话按它过滤：root 看全部，普通用户只看自己的。
    username: Mapped[str] = mapped_column(Text, nullable=False, default="")
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    answer: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(Text, nullable=False, default="全部")
    question_type: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 回答**当时**引用了什么的历史快照，刻意不加外键：
    # 文档被删除后这条记录仍然真实，把它"修复"掉等于销毁证据。
    source_docs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 回答**当时**引用到的**网络来源**（联网检索开启时才有）。
    #
    # 为什么不并进 ``source_docs``：两者的可信度与前端行为都不同——
    # 本地引用可以点开看切片并高亮原文，网络来源只能跳外链。
    # 混在一个数组里，前端就得靠字段猜测类型，而且
    # ``ChunkViewer`` 会拿到一个没有 document_id 的条目。
    web_sources: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        Index("qa_logs_created_at_idx", "created_at"),
        Index("qa_logs_session_idx", "session_id", "created_at"),
        Index("qa_logs_username_idx", "username"),
    )


class FeedbackLog(Base):
    """用户反馈。与 qa_logs 是 1:1，由 ``UNIQUE(qa_log_id)`` 表达并支撑 upsert。"""

    __tablename__ = "feedback_logs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    qa_log_id: Mapped[str] = mapped_column(
        Text, ForeignKey("qa_logs.id", ondelete="CASCADE"), nullable=False
    )
    rating: Mapped[str] = mapped_column(Text, nullable=False, default="unrated")
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    session_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[dt.datetime] = _now_column()
    updated_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        UniqueConstraint("qa_log_id", name="feedback_logs_qa_log_id_key"),
        CheckConstraint(
            "rating IN ('unrated', 'helpful', 'needs_improvement')",
            name="feedback_logs_rating_check",
        ),
        Index("feedback_logs_created_at_idx", "created_at"),
    )


# --------------------------------------------------------------------------- 可观测性


class RequestTrace(Base):
    """一次 HTTP 请求的轨迹。

    **只记"结构性"信息，不记内容**：路由模板、方法、状态码、耗时、用户名。
    刻意**不记** query string、请求体、header —— 那些里面会有口令、token、
    文档内容与提问原文。可观测性的目的是回答"慢在哪 / 错在哪"，
    不是留一份可以还原用户行为的副本。

    ``route`` 存的是**路由模板**（``/api/v1/pets/{name}``）而不是原始路径：
    否则每个不同的 id 都会变成一行独立指标，聚合页面会被打散成几千条。
    """
    __tablename__ = "request_traces"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    insert_seq: Mapped[int] = _seq_column()
    # 单次请求的关联 id（同一次请求的 LLM 调用用它串起来）
    request_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    route: Mapped[str] = mapped_column(Text, nullable=False, default="")
    method: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 谁发的。未登录请求为空串（登录/注册页）。**不记 token**。
    username: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 异常类名 + 简短信息。截断保存，避免把堆栈或敏感内容写进来。
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 冗余的汇总字段：列表页要显示"这个请求调了几次模型、花了多少 token"，
    # 冗余一份就免去对 llm_call_records 的 JOIN 与聚合。
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        Index("request_traces_created_at_idx", "created_at"),
        Index("request_traces_route_idx", "route"),
        Index("request_traces_username_idx", "username"),
        Index("request_traces_status_idx", "status"),
    )


class LlmCallRecord(Base):
    """一次模型调用的用量与耗时。

    ``agent`` 取自 LangGraph 的 ``metadata.langgraph_node``（已实测拿得到），
    所以能回答"五个 Agent 里哪个最贵"——这是本项目最实际的成本问题。

    ``request_id`` **刻意不加外键**：轨迹表会按保留策略被裁剪，如果这里有
    ``ON DELETE CASCADE``，删轨迹会连带删掉用量记录，成本统计就出现空洞。
    用量记录比轨迹更值得长期保留，留下孤儿行是更安全的取舍。
    """
    __tablename__ = "llm_call_records"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    insert_seq: Mapped[int] = _seq_column()
    request_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 节点名：planner / researcher / analyst / writer / reviewer / generate …
    agent: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # 这次调用是否**不**逐字推给前端（带 nostream 标签的结构化调用）。
    # 用来区分"用户看到的输出"与"内部推理开销"。
    structured: Mapped[bool] = mapped_column(nullable=False, default=False)
    # 拿不到 usage_metadata 时置真。**不猜数字**，让统计能区分
    # "确实用了 0 token" 与 "上游没返回用量"。
    usage_missing: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        Index("llm_call_records_created_at_idx", "created_at"),
        Index("llm_call_records_agent_idx", "agent"),
        Index("llm_call_records_request_idx", "request_id"),
    )


# 供 /system/categories 与前端上传校验共用，避免两处各写一份而漂移
EMBEDDING_DIM = settings.embedding_dim
