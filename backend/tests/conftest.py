"""pytest 夹具。

关键点：**测试不依赖任何外部服务**。
- 数据库用临时 SQLite 文件（不是 :memory:，因为要跨线程/连接共享）。
- 嵌入强制用 hash 提供者，并显式开启它——否则测试会因为本机没跑 ollama 而失败，
  变成"看环境决定能不能测"。
- 对话模型不真调：无依据路径本来就不调模型，有依据的路径在测试里用替身。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

# 测试库放 ``backend/data/test-tmp/``，**不能用 tempfile.mkdtemp()**。
#
# 原因：本机的系统临时目录（%TEMP%）对当前进程不可写，而 Python 的
# tempfile 在候选目录都写不了时会**回退到当前工作目录**——于是测试库
# 会以 ``ragkb-test-*`` 的名字堆在 backend/ 下，每次名字还不同，
# .gitignore 覆盖不到，很快就把工作区弄脏。
#
# ``backend/data/`` 已经在 .gitignore 里，测试产物放这里不会污染版本库；
# 整轮测试结束后下面的钩子会把它删掉。
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_TMP_DIR = _BACKEND_DIR / "data" / "test-tmp"
shutil.rmtree(_TMP_DIR, ignore_errors=True)
_TMP_DIR.mkdir(parents=True, exist_ok=True)

# 必须在 import app.* 之前设置环境变量：config 在 import 期就实例化 Settings
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP_DIR / 'test.db').as_posix()}"
os.environ["EMBEDDING_PROVIDER"] = "hash"
os.environ["EMBEDDING_DIM"] = "256"
# 至少 32 字节：PyJWT 对更短的 HMAC 密钥会告警（RFC 7518 §3.2）
os.environ["JWT_SECRET"] = "test-secret-not-for-production-0123456789"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "admin"
os.environ["LLM_API_KEY"] = ""
# checkpoint 库也要指向临时目录。不设的话会落到 backend/data/checkpoints.db，
# 于是测试线程**跨用例、跨整轮测试**残留：前一个用例的 findings 会被后一个
# 用例读到（thread_id 相同就命中），表现为"单独跑能过、全量跑就挂"。
os.environ["CHECKPOINT_DB"] = str(_TMP_DIR / "checkpoints.db")
# 联网检索在测试里一律关闭：真实调用会按次计费，而且让测试依赖外网。
# 需要验证联网路径的用例自己 monkeypatch（见 test_web_search.py）。
os.environ["TAVILY_API_KEY"] = ""
os.environ["WEB_SEARCH_ENABLED"] = "true"


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest 钩子签名
    """整轮测试结束后删掉临时目录。

    **必须先 dispose 引擎**：SQLite 的文件句柄还开着时，Windows 上删不掉
    那个 .db 文件，而 ``ignore_errors=True`` 会把这个失败吞掉，
    表现为"临时目录永远删不干净"。
    """
    try:
        from app.db.session import get_engine

        get_engine().dispose()
    except Exception:  # noqa: BLE001 - 清理阶段的失败不该让测试报错
        pass
    shutil.rmtree(_TMP_DIR, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolated_cache():
    """每个用例一个干净的 fakeredis + 清空的进程内缓存。

    **必须隔离**，否则用例之间会互相污染：登录限流是有状态的
    （``test_auth`` 里连续几次失败登录会让后一个用例一开始就被锁定），
    令牌吊销名单也是。这类污染的表现是"单独跑能过、全量跑就挂"，
    而且失败点与被污染的用例毫无关系，极难定位。

    用 fakeredis 而不是真实 Redis：测试不该依赖外部服务。
    """
    import fakeredis

    from app.cache import client as cache_client
    from app.cache import store as cache_store

    fake = fakeredis.FakeStrictRedis(decode_responses=False)
    cache_client.set_client_for_testing(fake)
    cache_store.reset_local_state()
    yield
    cache_client.set_client_for_testing(None)
    cache_client.reset_client()
    cache_store.reset_local_state()


@pytest.fixture(scope="session", autouse=True)
def _prepare_database():
    from app.db.session import init_db

    init_db()
    yield


@pytest.fixture(autouse=True)
def _isolated_checkpoint():
    """每个用例前重置 checkpointer。

    **必须重置**：checkpoint 是**跨轮持久**的，同一 thread_id 的后续调用
    会读到上一次的 ``turn_history`` / ``revision_count``。不隔离的话，
    用例之间会互相污染，表现为"某个用例单独跑是绿的、全量跑就红"，
    而且失败点看起来和 checkpoint 毫无关系。

    光重置内存单例**不够**——数据落在文件里，所以连文件一起删。
    删之前必须先关连接（Windows 上文件被占用时删不掉，而
    ``ignore_errors`` 会把这个失败吞掉，表现为"删了但没删掉"）。
    """
    from app.services import checkpoint, strategy_graph

    db_path = Path(os.environ["CHECKPOINT_DB"])

    def _reset():
        checkpoint.reset_saver()
        strategy_graph.reset_graph()
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _clean_tables():
    """每个用例前清空业务表，避免用例之间互相影响。"""
    from sqlalchemy import delete

    from app.db.models import (
        DocumentChunk,
        DocumentRecord,
        FeedbackLog,
        LlmCallRecord,
        QaLog,
        RequestTrace,
        UserRecord,
    )
    from app.db.session import transaction

    with transaction() as session:
        session.execute(delete(FeedbackLog))
        session.execute(delete(QaLog))
        session.execute(delete(DocumentChunk))
        session.execute(delete(DocumentRecord))
        session.execute(delete(UserRecord))
        # 可观测性两张表也要清：轨迹是**跨请求累积**的，
        # 不清的话前一个用例的请求会出现在后一个用例的总览里，
        # 表现为"数字对不上"这种极难定位的失败。
        session.execute(delete(LlmCallRecord))
        session.execute(delete(RequestTrace))
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def flush_traces():
    """把可观测性队列里的轨迹强制落库。

    轨迹是**异步**写的（后台线程批量落库），所以断言前必须显式 flush。
    **不要用 ``sleep`` 等**：既慢又不稳定（机器负载高时会偶发失败）。
    这个夹具直接调写入器提供的排空方法。
    """
    from app.observability import get_writer

    def _flush() -> None:
        get_writer().flush_now()

    return _flush


@pytest.fixture(scope="session")
def seeded_domain():
    """确保领域表有数据（精灵 / 阵容）。没有就导入种子。

    ★ **这个夹具必须在 conftest 里，不能放在某个测试文件里。**

    它原先定义在 ``test_roco.py`` 内部，于是只有那个文件能用；
    而 ``test_strategy.py`` 的 Researcher 节点要查本地库里的阵容与精灵——
    库里空着时它会走"无相关资料"的早退分支，**根本不调用模型**，
    于是 ``research_calls == 0``，扇出相关的断言全挂。

    这个依赖被**文件名顺序**掩盖了很久：pytest 按文件名收集，
    ``test_roco.py`` 排在 ``test_strategy.py`` 前面，先跑先播种，
    于是全量跑是绿的。一旦单独跑 ``test_strategy.py``、
    或插入一个排在前面的测试文件（例如 ``test_observability.py``），
    它立刻变红——而失败信息看起来像"扇出坏了"，把人引向完全错误的方向。

    放在 conftest 里 + ``scope="session"``，任何测试文件都能安全依赖它。
    """
    from app.services import roco_store

    status = roco_store.stats()
    if status["pets"] == 0 or status["lineups_total"] == 0:
        from app.scraper.seed_loader import seed_all

        seed_all()
    return roco_store.stats()


@pytest.fixture
def admin_token(client) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"username": "admin", "password": "admin"}
    )
    return response.json()["data"]["access_token"]


@pytest.fixture
def admin_headers(admin_token) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


def make_user(client, username: str, password: str = "secret123", approve: bool = True) -> dict:
    """建一个注册用户并（默认）审批通过，返回可用于请求的 headers。

    ``approve=False`` 时**不尝试登录**——未审批的账号登录本来就会失败，
    在那里取 token 只会得到一个 None 然后报 TypeError。
    """
    client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password, "display_name": username},
    )
    if not approve:
        return {}

    from app.services import users

    users.set_user_status(username, "approved")
    response = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    token = response.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def create_qa_log(
    session_id: str,
    username: str,
    question: str = "测试问题",
    answer: str = "测试回答",
    source_docs: list | None = None,
) -> dict:
    """直接写一条问答记录。

    会话归属、反馈这些用例只关心 qa_logs 里的数据，不该被迫经过一次真实问答
    （那需要模型，会把"能不能测"绑到环境上）。所以直接落库。
    """
    from app.services import storage

    return storage.add_qa_log(
        {
            "session_id": session_id,
            "username": username,
            "question": question,
            "answer": answer,
            "category": "全部",
            "question_type": "制度问答",
            "source_docs": source_docs or [],
        }
    )


@pytest.fixture
def user_headers(client) -> dict:
    return make_user(client, "alice")


@pytest.fixture
def other_user_headers(client) -> dict:
    return make_user(client, "bob")


@pytest.fixture
def sample_text() -> str:
    return (
        "年假制度：员工累计工作满 1 年不满 10 年的，年休假 5 天。\n"
        "年假最晚需要提前 3 个工作日申请，经直属主管审批后生效。\n"
        "报销差旅费需要提交出差申请单、交通票据、住宿发票与报销单。\n"
    )
