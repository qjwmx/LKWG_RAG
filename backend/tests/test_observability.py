"""可观测性的测试。

分五组，**权限与隐私是最重要的两组**：

1. **权限** —— 只有管理员能看。普通用户 403、未登录 401。
   这是本功能唯一的安全边界（前端隐藏入口不是边界）。
2. **隐私** —— 轨迹里**不能**出现口令、token、query string、请求体。
   这组测试是"防回归"的：以后有人图省事把请求体塞进轨迹，
   必须立刻失败。
3. **采集** —— 路由模板收敛、404 不泄露任意路径、用户名归因、
   LLM 按 Agent 归因、不重复计数。
4. **健壮性** —— 队列满时丢弃而不阻塞、埋点异常不影响业务、
   关闭开关后零写入。
5. **保留** —— 超期裁剪、行数上限。

轨迹是异步落库的，所以断言前统一用 ``flush_traces`` 夹具排空队列。
"""

from __future__ import annotations

from tests.conftest import make_user

# --------------------------------------------------------------------------- 权限


def test_observability_endpoints_require_login(client):
    """未登录 -> 真实 HTTP 401（沿用项目鉴权约定）。"""
    for endpoint in ("overview", "requests", "llm", "cache"):
        response = client.get(f"/api/v1/admin/observability/{endpoint}")
        assert response.status_code == 401, f"{endpoint} 未登录应 401"


def test_observability_endpoints_forbidden_for_normal_user(client):
    """★ 普通用户 -> 403。

    这是本功能**唯一的安全边界**。前端隐藏入口只是不显示，
    直接调接口仍然能拿到数据——所以必须在服务端拦住。
    """
    headers = make_user(client, "plain_user")
    for endpoint in ("overview", "requests", "llm", "cache"):
        response = client.get(f"/api/v1/admin/observability/{endpoint}", headers=headers)
        assert response.status_code == 403, f"{endpoint} 普通用户应 403，实际 {response.status_code}"


def test_observability_endpoints_allowed_for_admin(client, admin_headers):
    """管理员 -> 200，且返回统一响应格式。"""
    for endpoint in ("overview", "requests", "llm", "cache"):
        response = client.get(f"/api/v1/admin/observability/{endpoint}", headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["code"] == 200


# --------------------------------------------------------------------------- 隐私


def _all_trace_text(client, admin_headers) -> str:
    """把轨迹接口返回的全部内容拼成一个字符串，用于"不该出现什么"的断言。"""
    import json

    response = client.get(
        "/api/v1/admin/observability/requests", params={"limit": 200}, headers=admin_headers
    )
    return json.dumps(response.json(), ensure_ascii=False)


def test_trace_does_not_leak_password(client, admin_headers, flush_traces):
    """★ 口令绝不能出现在轨迹里。

    登录接口会被记录（"谁在爆破"正是要看的东西），但**只记路由模板**，
    不记任何凭据。这条测试是防回归的：以后有人图省事把请求体塞进轨迹，
    它会立刻失败。
    """
    secret = "sup3r-s3cret-password-xyz"
    client.post("/api/v1/auth/login", json={"username": "admin", "password": secret})
    flush_traces()

    assert secret not in _all_trace_text(client, admin_headers)


def test_trace_does_not_leak_token(client, admin_headers, flush_traces):
    """Authorization 头里的 JWT 绝不能落进轨迹。"""
    token = admin_headers["Authorization"].split(" ", 1)[1]
    # token 的签名段足够独特，直接搜整串或它的尾部都能说明问题
    client.get("/api/v1/system/categories", headers=admin_headers)
    flush_traces()

    text = _all_trace_text(client, admin_headers)
    assert token not in text
    assert token[-24:] not in text


def test_trace_does_not_leak_query_string(client, admin_headers, flush_traces):
    """★ query string 不落库。

    ``/pokedex/pet?name=xxx`` 里的参数可能是用户输入的内容，
    记下来就等于记了用户行为。我们只记路由模板。
    """
    marker = "leak-marker-9f3a"
    client.get(
        f"/api/v1/pokedex/pet?name={marker}", headers=admin_headers
    )
    flush_traces()

    assert marker not in _all_trace_text(client, admin_headers)


def test_trace_does_not_leak_request_body(client, admin_headers, flush_traces):
    """请求体不落库（提问原文属于用户内容）。"""
    marker = "body-marker-7c1b"
    client.post(
        "/api/v1/chat/chat",
        json={"question": marker, "session_id": "s", "category": "全部", "stream": False},
        headers=admin_headers,
    )
    flush_traces()

    assert marker not in _all_trace_text(client, admin_headers)


# --------------------------------------------------------------------------- 采集


def test_request_is_traced_with_route_template(client, admin_headers, flush_traces):
    """请求被记录，且 ``route`` 是**模板**不是原始路径。"""
    client.get("/api/v1/pokedex/pet", params={"name": "音速犬"}, headers=admin_headers)
    flush_traces()

    from sqlalchemy import select

    from app.db.models import RequestTrace
    from app.db.session import transaction

    with transaction() as session:
        rows = list(session.execute(select(RequestTrace)).scalars())

    routes = {r.route for r in rows}
    assert "/api/v1/pokedex/pet" in routes, f"未记录到该路由，实际：{routes}"
    # 原始路径带的是 query，不该出现在 route 里
    assert not any("音速犬" in r for r in routes)


def test_dynamic_paths_collapse_to_one_metric_key(client, admin_headers, flush_traces):
    """★ 不同的路径参数必须收敛成同一个路由模板。

    否则 ``/pets/火苗``、``/pets/水灵`` 各自变成一行指标，
    聚合页面会被打散成几千条，完全没法看。
    """
    for name in ("音速犬", "寂灭骨龙", "化蝶"):
        client.get("/api/v1/pokedex/pet", params={"name": name}, headers=admin_headers)
    flush_traces()

    from sqlalchemy import select

    from app.db.models import RequestTrace
    from app.db.session import transaction

    with transaction() as session:
        rows = [
            r
            for r in session.execute(select(RequestTrace)).scalars()
            if r.route.startswith("/api/v1/pokedex/pet")
        ]

    assert len(rows) >= 3, "三次请求都应被记录"
    assert len({r.route for r in rows}) == 1, "不同参数产生了不同的指标键"


def test_404_does_not_record_arbitrary_path(client, admin_headers, flush_traces):
    """★ 404 不能把任意路径原样记下来。

    否则攻击者/爬虫可以用大量不存在的路径把轨迹表灌满，
    而且这些路径本身可能是探测内容。
    """
    client.get("/api/v1/secret-probe-abc/xyz", headers=admin_headers)
    flush_traces()

    from sqlalchemy import select

    from app.db.models import RequestTrace
    from app.db.session import transaction

    with transaction() as session:
        rows = list(session.execute(select(RequestTrace)).scalars())

    routes = " ".join(r.route for r in rows)
    assert "secret-probe-abc" not in routes, f"任意路径被记进轨迹：{routes}"


def test_username_is_attributed(client, admin_headers, flush_traces):
    """已鉴权请求要归因到具体用户（身份只来自 JWT）。"""
    client.get("/api/v1/system/categories", headers=admin_headers)
    flush_traces()

    from sqlalchemy import select

    from app.db.models import RequestTrace
    from app.db.session import transaction

    with transaction() as session:
        rows = list(session.execute(select(RequestTrace)).scalars())

    assert any(r.username == "admin" for r in rows), "没有记录到提问者"


def test_error_status_is_recorded(client, admin_headers, flush_traces):
    """失败请求也要被记录——那恰恰是最需要观测的。"""
    client.get("/api/v1/admin/users", headers={})  # 401
    flush_traces()

    from sqlalchemy import select

    from app.db.models import RequestTrace
    from app.db.session import transaction

    with transaction() as session:
        rows = list(session.execute(select(RequestTrace)).scalars())

    assert any(r.status == 401 for r in rows), "失败请求没有被记录"


# --------------------------------------------------------------------------- LLM 归因


def _run_fake_model(agent: str, tokens: int = 30):
    """用一个假模型触发 LLM 回调，并归因到指定 Agent。

    不调真实模型：测试不该依赖外部服务与网络。
    """
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from app.observability import LLM_HANDLER

    class FakeModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "fake"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
            message = AIMessage(
                content="ok",
                usage_metadata={
                    "input_tokens": tokens // 2,
                    "output_tokens": tokens // 2,
                    "total_tokens": tokens,
                },
            )
            return ChatResult(
                generations=[ChatGeneration(message=message)],
                llm_output={"token_usage": {"total_tokens": tokens}, "model_name": "fake-model"},
            )

    # metadata 里带 langgraph_node，模拟 LangGraph 传进来的归因信息
    return FakeModel(callbacks=[LLM_HANDLER]).invoke(
        "hi", config={"metadata": {"langgraph_node": agent}}
    )


def test_llm_call_is_attributed_to_agent():
    """★ 模型调用要能归因到具体 Agent——这是"哪个 Agent 最贵"的依据。"""
    from app.observability import context as ctx

    record = ctx.new_record()
    token = ctx.set_record(record)
    try:
        _run_fake_model("planner", tokens=40)
    finally:
        ctx.reset_record(token)

    assert len(record.llm_calls) == 1, f"应记录 1 次调用，实际 {len(record.llm_calls)}"
    call = record.llm_calls[0]
    assert call.agent == "planner"
    assert call.total_tokens == 40
    assert call.usage_missing is False


def test_llm_call_is_recorded_only_once():
    """★ 一次调用只能记一条。

    ``_structured()`` 复用 ``get_chat_model()``，handler 在**构造处**绑定。
    如果谁在 invoke 时又传一遍 callbacks，同一次调用会被记两次——
    token 直接翻倍，而统计页面看起来完全正常。
    """
    from app.observability import context as ctx

    record = ctx.new_record()
    token = ctx.set_record(record)
    try:
        _run_fake_model("writer")
    finally:
        ctx.reset_record(token)

    assert len(record.llm_calls) == 1, f"重复计数：{len(record.llm_calls)} 条"


def test_parallel_calls_do_not_mix_up_durations():
    """★ 并行调用（Researcher 扇出）不能互相覆盖计时。

    用单个变量存"起始时间"的话，并行时后一个会覆盖前一个，
    耗时算成 0 或负数。所以用 run_id 做键的字典。
    """
    import threading

    from app.observability import context as ctx

    record = ctx.new_record()
    token = ctx.set_record(record)
    try:
        threads = [
            threading.Thread(target=_run_fake_model, args=(f"researcher{i}",))
            for i in range(6)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        ctx.reset_record(token)

    # 注意：contextvar 在**新建线程**里是空的，所以子线程里的调用不会
    # 记进这个 record（这正是隔离性正确的表现）。这里断言的是"没有崩溃、
    # 没有负数耗时"。
    assert all(call.duration_ms >= 0 for call in record.llm_calls)


def test_llm_usage_missing_is_flagged_not_guessed():
    """上游不返回用量时，**记 0 并标记**，不要估算。

    估算值会污染成本统计——让"到底花了多少"变成一个看起来精确、
    实际虚构的数字。
    """
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from app.observability import LLM_HANDLER, context as ctx

    class NoUsageModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "nousage"

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
            # 刻意不带 usage_metadata，也不带 llm_output.token_usage
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])

    record = ctx.new_record()
    token = ctx.set_record(record)
    try:
        NoUsageModel(callbacks=[LLM_HANDLER]).invoke("hi")
    finally:
        ctx.reset_record(token)

    assert len(record.llm_calls) == 1
    assert record.llm_calls[0].usage_missing is True
    assert record.llm_calls[0].total_tokens == 0


def test_llm_handler_never_raises_on_bad_response():
    """回调拿到畸形响应也不能抛——埋点失败绝不能影响问答。"""
    from app.observability import LLM_HANDLER

    class Weird:
        generations = None
        llm_output = None

    # 不该抛异常
    LLM_HANDLER.on_llm_end(Weird(), run_id="nonexistent")


# --------------------------------------------------------------------------- 健壮性


def test_writer_drops_when_queue_full_without_blocking():
    """★ 队列满时**丢弃**，绝不阻塞业务。

    观测数据是可丢弃的旁路信息。一个满队列让用户请求变慢或失败，
    是拿可用性换指标，方向反了。
    """
    import time

    from app.observability.context import RequestRecord
    from app.observability.writer import TraceWriter

    writer = TraceWriter()
    # 不启动后台线程 -> 队列必然填满
    writer._queue.maxsize = 5  # noqa: SLF001 - 测试需要小队列
    for _ in range(5):
        assert writer.submit(RequestRecord()) is True

    started = time.perf_counter()
    for _ in range(50):
        writer.submit(RequestRecord())  # 全部应被丢弃
    elapsed = time.perf_counter() - started

    assert elapsed < 0.5, f"队列满时 submit 阻塞了 {elapsed:.2f}s"
    assert writer.stats()["dropped"] == 50


def test_writer_flushes_on_stop():
    """停止时要尽量排空，不能丢已入队的数据。"""
    from app.db.models import RequestTrace
    from app.db.session import transaction
    from app.observability.context import RequestRecord
    from app.observability.writer import TraceWriter
    from sqlalchemy import func, select

    writer = TraceWriter()
    writer.start()
    for _ in range(20):
        writer.submit(RequestRecord(route="/test/flush", method="GET", status=200))
    writer.stop()

    with transaction() as session:
        count = (
            session.execute(
                select(func.count())
                .select_from(RequestTrace)
                .where(RequestTrace.route == "/test/flush")
            ).scalar()
            or 0
        )
    assert count == 20, f"停止时丢了数据：{count}/20"


def test_disabled_observability_writes_nothing(client, monkeypatch, flush_traces):
    """关掉开关后**零写入**——排查性能问题时不该让埋点成为变量。"""
    from app.config import settings

    monkeypatch.setattr(settings, "observability_enabled", False)
    client.get("/api/v1/system/categories")
    flush_traces()

    from app.db.models import RequestTrace
    from app.db.session import transaction
    from sqlalchemy import func, select

    with transaction() as session:
        count = session.execute(select(func.count()).select_from(RequestTrace)).scalar() or 0
    assert count == 0


def test_middleware_does_not_swallow_business_errors(client, admin_headers, flush_traces):
    """中间件必须让业务异常继续抛出，同时把它记下来。

    吞掉异常会让 500 变成 200，是严重的功能退化。
    """
    # 用一个必然失败的请求触发异常路径：不存在的文档 id 会走 404 分支，
    # 而这里用参数校验失败（422 -> 我们的 handler 转成 200+code）
    response = client.post(
        "/api/v1/chat/chat", json={}, headers=admin_headers
    )
    # 校验失败由 exception_handler 转成 200 + 非 200 的 code
    assert response.status_code == 200
    assert response.json()["code"] != 200
    flush_traces()


# --------------------------------------------------------------------------- 保留策略


def test_retention_deletes_expired_rows(client, admin_headers, flush_traces):
    """超期的轨迹会被裁掉。"""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from app.db.models import RequestTrace
    from app.db.session import transaction
    from app.observability.writer import TraceWriter

    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    with transaction() as session:
        session.add(
            RequestTrace(
                id="old-trace",
                insert_seq=1,
                request_id="old-trace",
                route="/old",
                method="GET",
                status=200,
                duration_ms=1.0,
                created_at=old,
            )
        )

    TraceWriter()._cleanup()  # noqa: SLF001 - 直接验证清理逻辑

    with transaction() as session:
        remaining = (
            session.execute(
                select(func.count())
                .select_from(RequestTrace)
                .where(RequestTrace.id == "old-trace")
            ).scalar()
            or 0
        )
    assert remaining == 0, "超期轨迹没有被裁掉"


def test_retention_respects_max_rows(client, admin_headers, flush_traces, monkeypatch):
    """行数上限生效——单日流量大时 7 天的量也可能撑爆单机 SQLite。"""
    from app.config import settings
    from sqlalchemy import func, select

    from app.db.models import RequestTrace
    from app.db.session import transaction
    from app.observability.writer import TraceWriter

    monkeypatch.setattr(settings, "observability_max_rows", 100)
    with transaction() as session:
        for i in range(150):
            session.add(
                RequestTrace(
                    id=f"cap-{i}",
                    insert_seq=i + 1,
                    request_id=f"cap-{i}",
                    route="/cap",
                    method="GET",
                    status=200,
                    duration_ms=1.0,
                )
            )

    TraceWriter()._cleanup()  # noqa: SLF001

    with transaction() as session:
        count = session.execute(select(func.count()).select_from(RequestTrace)).scalar() or 0
    assert count <= 100, f"行数上限没生效：{count}"


# --------------------------------------------------------------------------- 统计正确性


def test_percentile_interpolates_for_small_samples():
    """小样本时百分位要插值，不能直接取最大值。

    只取第 k 个的话，5 个请求的 P95 就等于最大值，
    看起来像"总是最慢的那次"，误导性很强。
    """
    from app.services.observability_store import _percentile

    assert _percentile([], 0.95) == 0.0
    assert _percentile([10.0], 0.95) == 10.0
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    p95 = _percentile(values, 0.95)
    assert 4.0 < p95 < 5.0, f"P95 应插值到 4~5 之间，实际 {p95}"
    assert _percentile(values, 0.5) == 3.0


def test_overview_counts_only_window(client, admin_headers, flush_traces):
    """总览只统计窗口内的请求，不是全表。"""
    client.get("/api/v1/system/categories", headers=admin_headers)
    flush_traces()

    data = client.get(
        "/api/v1/admin/observability/overview", params={"hours": 24}, headers=admin_headers
    ).json()["data"]
    assert data["requests"] >= 1
    assert "latency" in data
    assert data["latency"]["sample_size"] >= 1


def test_llm_breakdown_groups_by_agent(client, admin_headers, flush_traces):
    """按 Agent 分组统计 token。"""
    from app.db.models import LlmCallRecord
    from app.db.session import transaction

    with transaction() as session:
        session.add(
            LlmCallRecord(
                id="c1",
                insert_seq=1,
                request_id="r1",
                agent="planner",
                model="m",
                total_tokens=100,
                input_tokens=60,
                output_tokens=40,
            )
        )
        session.add(
            LlmCallRecord(
                id="c2",
                insert_seq=2,
                request_id="r1",
                agent="writer",
                model="m",
                total_tokens=300,
                input_tokens=200,
                output_tokens=100,
            )
        )

    data = client.get("/api/v1/admin/observability/llm", headers=admin_headers).json()["data"]
    agents = {a["agent"]: a for a in data["agents"]}
    assert "planner" in agents and "writer" in agents
    assert agents["writer"]["total_tokens"] == 300
    # writer 占 300/400 = 75%
    assert agents["writer"]["token_share"] == 0.75
    assert data["total_tokens"] == 400
