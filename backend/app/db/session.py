"""引擎与会话。

**引擎必须惰性创建**，绝不在 import 期执行 ``create_engine``：本模块被所有
service import，一旦在 import 期连库，数据库一抖动整个应用连页面都渲染不出来，
而且报错位置极具误导性。

SQLite 需要两个特殊处理：
- ``check_same_thread=False``：FastAPI 把 ``def`` 端点丢进线程池，连接会跨线程。
- ``PRAGMA foreign_keys=ON``：SQLite 默认**不**强制外键，不开的话
  ``ON DELETE CASCADE`` 形同虚设——删文档后向量会静默残留。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import ensure_runtime_dirs, settings

logger = logging.getLogger(__name__)

_engine = None
_SessionLocal: sessionmaker | None = None


def _build_engine():
    url = settings.database_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # 相对路径的 sqlite URL 需要目录先存在
        ensure_runtime_dirs()
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # 长时间空闲后 PostgreSQL 会掐断连接，pre_ping + recycle 是必需的
        kwargs.update(pool_size=5, max_overflow=10, pool_recycle=1800)
    return create_engine(url, **kwargs)


def get_engine():
    global _engine
    if _engine is None:
        _engine = _build_engine()
        if settings.database_url.startswith("sqlite"):

            @event.listens_for(_engine, "connect")
            def _configure_sqlite(dbapi_connection, connection_record):  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                # WAL 是**性能前提**，不是优化：SQLite 默认的 DELETE journal
                # 每次 commit 都要 fsync 并重写整个 journal，实测单条 INSERT
                # **2.05ms**；WAL 下只要 **0.022ms**（差 90 倍）。
                #
                # 为什么必须在这里设：可观测性埋点要给每个请求写一行轨迹，
                # 按 2.05ms 算会把 3ms 的接口直接拖慢一倍。WAL 让后台写入器
                # 的批量落库几乎免费，也顺带加速现有的上传与问答落库。
                #
                # 它是**持久**属性（写在库文件头），但每次连接设一遍无副作用
                # ——已处于 WAL 时该 PRAGMA 立即返回 'wal'。
                # 必须在事务外执行，连接建立时正是事务外。
                cursor.execute("PRAGMA journal_mode=WAL")
                # NORMAL 在 WAL 下是安全的：崩溃可能丢最后几个已提交事务，
                # 但**不会损坏数据库**（这正是 WAL 的设计保证）。
                # 埋点数据丢几条无关紧要，不值得为它付 FULL 的 fsync 成本。
                cursor.execute("PRAGMA synchronous=NORMAL")
                # 大语句（例如批量插入上千行）超出内存预算时，SQLite 会**落盘**
                # 到临时文件。临时目录不可写时（受限环境、容器只读 tmp）会报
                # 一个极具误导性的 "unable to open database file" —— 看起来像
                # 数据库文件有问题，实际是临时目录的问题。实测在本机沙箱里
                # 单条语句超过约 180 行就会触发。
                #
                # MEMORY 让临时数据留在内存里，彻底绕开这个坑，同时也更快。
                # 代价是极大排序会占内存，但本项目的临时数据量很小。
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.close()

    return _engine


def get_session_factory() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def transaction():
    """一次事务。

    服务层持有的是 ``sessionmaker`` 而不是 ``Session``：``Session`` 不是线程安全的，
    而端点跑在线程池里、每个请求都会新建一个 service 实例。
    """
    session: Session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """建表。幂等，可重复执行。

    用 ``create_all`` 而不是 Alembic：本项目是单机起步的完整交付，
    引入迁移工具会让"三条命令起服务"变成"先 alembic upgrade head"。
    schema 变更时删库重建即可（数据在 data/ 下，本就是开发期资产）。

    **但 ``create_all`` 不会给已存在的表加列。** 删库重建在开发期可以，
    而阵容表里已经有几百套抓来的数据、重建要重跑十几分钟爬虫，
    所以下面有一个针对性的补列步骤（见 ``_ensure_columns``）。
    """
    from app.db import domain_models, models  # noqa: F401
    # 两个模块都要 import：模型只有被 import 过才会注册到 Base.metadata，
    # 漏掉任何一个都会静默少建一批表（create_all 不会报错，只是不建）。

    ensure_runtime_dirs()
    Base = models.Base
    Base.metadata.create_all(bind=get_engine())
    _ensure_columns()


# 已发布之后新增的列。**必须在这里登记**，否则老库上跑起来会
# ``no such column`` —— 而且是运行时才炸，建表阶段完全正常。
# 格式：``表名 -> {列名: 建列 DDL 片段}``
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "lineups": {
        # 游戏内阵容码（B 站源独有）
        "import_code": "TEXT NOT NULL DEFAULT ''",
    },
    "qa_logs": {
        # 回答当时引用到的网络来源（联网检索开启时才有）。
        # JSON 列在 SQLite 上存 TEXT，所以默认值用 '[]' 而不是 '{}'。
        "web_sources": "JSON NOT NULL DEFAULT '[]'",
    },
}


def _ensure_columns() -> None:
    """给已存在的表补上后加的列（SQLite / PostgreSQL 通用的 ADD COLUMN）。

    只做"加列"，不做改类型或删列——那两类操作没法用一条幂等 DDL 安全完成，
    真需要时应该上真正的迁移工具。
    """
    from sqlalchemy import inspect, text

    engine = get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table, columns in _ADDED_COLUMNS.items():
        if table not in existing_tables:
            continue  # 新库由 create_all 直接建成最终形状
        present = {column["name"] for column in inspector.get_columns(table)}
        missing = {name: ddl for name, ddl in columns.items() if name not in present}
        if not missing:
            continue
        with engine.begin() as connection:
            for name, ddl in missing.items():
                connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        logger.info("补齐列：%s -> %s", table, ", ".join(sorted(missing)))


def check_connection() -> tuple[bool, str]:
    """健康检查用。返回 ``(是否可用, 说明)``，不抛异常。"""
    from sqlalchemy import text

    try:
        with transaction() as session:
            session.execute(text("SELECT 1"))
        return True, "连接正常"
    except Exception as exc:  # noqa: BLE001 - 健康检查要把异常转成可读描述
        return False, f"不可用（{type(exc).__name__}: {exc}）"
