"""问答链路（五 Agent 检索流水线）与 SSE 帧。

核心验证点：
1. 无依据时**不调用 LLM**，但仍要落库（否则用户看不到自己问过什么）。
2. SSE 帧的形状是前后端契约：delta 可拼接、done 带引用与 chunk_index。
3. 检索的可见性过滤必须下推——普通用户搜不到他人私有文档。
4. 五 Agent 骨架真的在跑：Planner 拆解、Researcher 并行扇出、归约不丢结果。

替身策略：把 ``rag_graph.get_chat_model`` 换成假模型。
它是**模块级函数、在节点内被调用**，所以 monkeypatch 生效；
不这样做的话测试就得真调外部服务，"能不能测"会变成看环境。

假模型必须同时提供 ``invoke`` 与 ``with_structured_output``：
Planner/Researcher/Reviewer 走结构化输出，只实现 ``invoke`` 的话
这三个节点全部退化到兜底分支，测的就只是"兜底能不能跑"。
"""

from __future__ import annotations

import io

import pytest

from tests.conftest import create_qa_log


def upload(client, headers, name: str, text: str, category: str = "阵容攻略"):
    return client.post(
        "/api/v1/knowledge_base/upload_docs",
        files=[("files", (name, io.BytesIO(text.encode("utf-8")), "text/markdown"))],
        data={"category": category, "version": "v1.0", "title": ""},
        headers=headers,
    )


def parse_sse(raw: str) -> list[dict]:
    """把 SSE 响应体解析成事件列表。"""
    import json

    events = []
    for block in raw.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload:
                    events.append(json.loads(payload))
    return events


def of_type(events: list[dict], kind: str) -> list[dict]:
    """按 type 过滤事件。

    **不能用 events[0] 断言正文**：五 Agent 会先发 agent_status/artifact
    进度帧，正文 delta 出现在它们之后。按类型过滤才不会被帧顺序绑死。
    """
    return [e for e in events if e["type"] == kind]


class FakeScript:
    """结构化输出的脚本。"""

    def __init__(
        self, units: int = 2, verdict: str = "pass", factual_issue: bool = False
    ) -> None:
        self.units = units
        self.verdict = verdict
        self.factual_issue = factual_issue
        self.plan_calls = 0
        self.research_calls = 0
        self.reviews = 0

    def structured(self, schema):
        name = schema.__name__
        if name == "ResearchPlan":
            self.plan_calls += 1
            unit_cls = schema.model_fields["units"].annotation.__args__[0]
            return schema(
                brief="测试研究简报",
                units=[
                    unit_cls(topic=f"子问题{i}", reason=f"理由{i}") for i in range(self.units)
                ],
            )
        if name == "Finding":
            self.research_calls += 1
            return schema(
                unit_id="u0",
                topic="子问题",
                summary="检索到的结论",
                evidence=["制度.md（第 0 段）"],
            )
        if name == "ReviewVerdict":
            self.reviews += 1
            return schema(
                verdict=self.verdict,
                notes="",
                evidence_gaps=[],
                factual_issue=self.factual_issue,
            )
        raise AssertionError(f"未预期的 schema：{name}")


class FakeStructured:
    def __init__(self, schema, script: FakeScript) -> None:
        self.schema = schema
        self.script = script

    def invoke(self, _messages, **_kwargs):
        return self.script.structured(self.schema)


class FakeModel:
    """替身对话模型。

    ``invoke`` 返回真正的 AIMessage：返回自定义对象的话 LangGraph 的
    messages 流识别不了，测试就只验证了兜底路径。
    """

    def __init__(
        self,
        text: str = "### 最终回答\n年假需提前 3 个工作日申请。",
        script: FakeScript | None = None,
    ) -> None:
        self.text = text
        self.script = script or FakeScript()
        self.calls = 0

    def with_structured_output(self, schema, **_kwargs):
        return FakeStructured(schema, self.script)

    def invoke(self, messages, **_kwargs):
        from langchain_core.messages import AIMessage

        self.calls += 1
        return AIMessage(content=self.text)


def patch_model(monkeypatch, model=None):
    """把图里的模型工厂换成替身，返回替身实例以便断言调用次数。"""
    from app.services import rag_graph

    fake = model or FakeModel()
    monkeypatch.setattr(rag_graph, "get_chat_model", lambda tags=None, temperature=None: fake)
    return fake


# --------------------------------------------------------------------------- 无依据路径


def test_no_evidence_does_not_call_llm(client, admin_headers, monkeypatch):
    """知识库为空时直接返回"资料不足"，**不调模型**，但仍落库。

    预检（COUNT）为空要短路掉整个五 Agent 流水线——否则会花
    1 次 Planner + N 次 Researcher 的调用才得出"没资料"。
    """
    fake = patch_model(monkeypatch)

    response = client.post(
        "/api/v1/chat/chat",
        json={
            "question": "年假怎么申请？",
            "session_id": "s-empty",
            "category": "全部",
            "stream": False,
        },
        headers=admin_headers,
    )
    body = response.json()
    assert body["code"] == 200
    assert "资料不足" in body["data"]["answer"]
    assert body["data"]["source_docs"] == []
    assert body["data"]["qa_log_id"]

    # 关键断言：模型一次都没被调用（Planner 也没跑）
    assert fake.calls == 0
    assert fake.script.plan_calls == 0, "预检为空时不该启动 Planner"

    # 仍然落库
    from app.services import storage

    assert len(storage.list_qa_logs()) == 1


def test_no_evidence_stream_emits_delta_then_done(client, admin_headers, monkeypatch):
    fake = patch_model(monkeypatch)

    response = client.post(
        "/api/v1/chat/chat",
        json={
            "question": "不存在的问题",
            "session_id": "s-empty2",
            "category": "全部",
            "stream": True,
        },
        headers=admin_headers,
    )
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    # 关掉中间层缓冲，否则流式会变成一次性吐出
    assert response.headers.get("x-accel-buffering") == "no"

    events = parse_sse(response.text)
    deltas = of_type(events, "delta")
    done = of_type(events, "done")

    assert deltas, "无依据也要发 delta，否则界面一片空白"
    assert "资料不足" in "".join(e["content"] for e in deltas)
    assert done[-1]["source_docs"] == []
    # done 必须在所有 delta 之后：正文流完才写库
    assert events.index(done[-1]) > events.index(deltas[-1])
    assert fake.calls == 0


# --------------------------------------------------------------------------- 有依据路径


@pytest.fixture
def seeded(client, admin_headers, sample_text):
    upload(client, admin_headers, "制度.md", sample_text)
    return admin_headers


def test_question_type_is_inferred(client, seeded, monkeypatch):
    """题型由关键词硬匹配，**不依赖模型**。"""
    patch_model(monkeypatch)

    cases = {
        "推荐一套阵容": "阵容推荐",
        "怎么克制幽系？": "针对克制",
        "帮我总结这个版本的改动": "版本总结",
        "寂灭骨龙是什么属性？": "机制问答",
    }
    for question, expected in cases.items():
        response = client.post(
            "/api/v1/chat/chat",
            json={"question": question, "session_id": "s-type", "category": "全部", "stream": False},
            headers=seeded,
        )
        assert response.json()["data"]["question_type"] == expected


def test_stream_done_carries_citation_fields(client, seeded, monkeypatch):
    """done 事件必须带 document_id 与 chunk_index —— 前端靠它做引用高亮。"""
    patch_model(monkeypatch)

    response = client.post(
        "/api/v1/chat/chat",
        json={
            "question": "年假最晚提前几天申请？",
            "session_id": "s-cite",
            "category": "全部",
            "stream": True,
        },
        headers=seeded,
    )
    events = parse_sse(response.text)

    deltas = of_type(events, "delta")
    done = of_type(events, "done")[-1]

    # delta 可拼接出正文
    joined = "".join(e.get("content", "") for e in deltas)
    assert joined

    sources = done["source_docs"]
    assert sources, "有依据时必须给出引用"
    for source in sources:
        assert source["document_id"]
        assert isinstance(source["chunk_index"], int)
        assert source["snippet"]
        assert source["title"]

    # done 里的 answer 是落库的完整回答（含引用段落），前端用它回填
    assert "引用文档" in done["answer"]


def test_answer_is_persisted_with_sources(client, seeded, monkeypatch):
    from app.services import storage

    patch_model(monkeypatch)

    client.post(
        "/api/v1/chat/chat",
        json={"question": "年假？", "session_id": "s-persist", "category": "全部", "stream": True},
        headers=seeded,
    )

    logs = storage.list_qa_logs()
    assert len(logs) == 1
    assert logs[0]["session_id"] == "s-persist"
    assert logs[0]["username"] == "admin"
    assert logs[0]["source_docs"], "落库的 source_docs 不能为空"


def test_model_failure_is_reported_and_not_persisted(client, seeded, monkeypatch):
    """模型报错时要给出可读错误，且**不落库**。

    把残缺回答写进历史会污染后续轮次的上下文
    （下一轮会把这段残话当成"之前说过的话"）。
    """
    from app.services import storage

    class ExplodingModel(FakeModel):
        def invoke(self, messages, **_kwargs):
            raise RuntimeError("模拟模型不可用")

    patch_model(monkeypatch, ExplodingModel())

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假？", "session_id": "s-fail", "category": "全部", "stream": False},
        headers=seeded,
    )
    # 非流式：service 把图里的 error 抛出来，路由转成可读的 code != 200
    assert response.json()["code"] != 200
    assert storage.list_qa_logs() == []


def test_stream_model_failure_emits_error_event(client, seeded, monkeypatch):
    from app.services import storage

    class ExplodingModel(FakeModel):
        def invoke(self, messages, **_kwargs):
            raise RuntimeError("模拟模型不可用")

    patch_model(monkeypatch, ExplodingModel())

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假？", "session_id": "s-fail2", "category": "全部", "stream": True},
        headers=seeded,
    )
    events = parse_sse(response.text)
    assert any(e["type"] == "error" for e in events)
    assert storage.list_qa_logs() == []


# --------------------------------------------------------------------------- 五 Agent 骨架


@pytest.fixture
def big_kb(client, admin_headers):
    """足够大的知识库，用来触发 Planner 拆解。

    为什么要单独一个夹具：``PLANNER_SKIP_THRESHOLD`` 会在切片很少时
    跳过 Planner（小库拆解没有信息增益，白花约 1.7 秒）。
    扇出与归约的用例必须先跨过这个阈值，否则测的是"跳过"分支。
    """
    for index in range(4):
        upload(
            client,
            admin_headers,
            f"制度{index}.md",
            f"第 {index} 份材料：年假需要提前 {index + 3} 个工作日申请，"
            f"经直属主管审批后生效。报销需要提交申请单与票据。",
        )
    return admin_headers


def test_planner_is_skipped_for_tiny_knowledge_base(client, seeded, monkeypatch):
    """★ 小知识库跳过 Planner —— 这是首屏延迟的主要来源之一。

    实测 Planner 是一次独立的结构化 LLM 调用（约 1.7 秒），
    而库里只有一两个切片时，拆出来的每个子问题都会命中同一批内容，
    那次调用没有任何信息增益。
    """
    from app.services import rag_graph

    fake = patch_model(monkeypatch, FakeModel(script=FakeScript(units=3)))

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假怎么算？", "session_id": "s-skip", "category": "全部", "stream": True},
        headers=seeded,
    )
    events = parse_sse(response.text)

    assert fake.script.plan_calls == 0, "小库不该调 Planner"
    assert fake.script.research_calls == 1, "应退化为单单元检索"

    stages = {e.get("stage") for e in of_type(events, "agent_status") if e["agent"] == "Planner"}
    assert "skipped" in stages, f"应发出 skipped 状态，实际 {stages}"

    # 仍然要给出 plan artifact，前端才能画出流水线
    plans = [e for e in of_type(events, "artifact") if e.get("kind") == "plan"]
    assert plans and plans[0]["payload"]["skipped"] is True

    done = of_type(events, "done")[-1]
    assert done["units"] == [
        {"unit_id": "u0", "topic": "年假怎么算？", "reason": "知识库较小，直接检索原问题"}
    ]
    assert rag_graph.PLANNER_SKIP_THRESHOLD >= 1


def test_planner_fans_out_one_researcher_per_unit(client, big_kb, monkeypatch):
    """★ Send 扇出必须真的发生。

    没接对的话 planner 会直接连到 researcher、带完整 state 跑一次
    （topic 为空），Send 永远不触发——图能跑完，但只有一次检索。
    """
    fake = patch_model(monkeypatch, FakeModel(script=FakeScript(units=3)))

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假怎么算？", "session_id": "s-fan", "category": "全部", "stream": True},
        headers=big_kb,
    )
    events = parse_sse(response.text)

    assert fake.script.plan_calls == 1, "Planner 应只跑一次"
    assert fake.script.research_calls == 3, f"应有 3 次检索，实际 {fake.script.research_calls}"

    units = {
        e.get("unit_id")
        for e in of_type(events, "agent_status")
        if e.get("agent") == "Researcher" and e.get("unit_id")
    }
    assert len(units) == 3, f"应有 3 个不同单元，实际 {units}"


def test_findings_are_reduced_not_overwritten(client, big_kb, monkeypatch):
    """★ 归约：并行的多条发现都要保留。

    ``findings`` 少了 ``operator.add`` 时，后完成的研究会覆盖先完成的，
    最终只剩一条——而且不报错。
    """
    patch_model(monkeypatch, FakeModel(script=FakeScript(units=3)))

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假怎么算？", "session_id": "s-red", "category": "全部", "stream": True},
        headers=big_kb,
    )
    done = of_type(parse_sse(response.text), "done")[-1]
    assert len(done["findings"]) == 3, f"3 个单元应产出 3 条发现，实际 {len(done['findings'])}"
    assert len(done["units"]) == 3


def test_units_are_capped(client, big_kb, monkeypatch):
    """研究单元数量有上限，避免扇出过多拖长耗时。"""
    from app.services import rag_graph

    fake = patch_model(monkeypatch, FakeModel(script=FakeScript(units=10)))

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假怎么算？", "session_id": "s-cap", "category": "全部", "stream": True},
        headers=big_kb,
    )
    done = of_type(parse_sse(response.text), "done")[-1]
    assert len(done["units"]) <= rag_graph.MAX_RESEARCH_UNITS
    assert fake.script.research_calls <= rag_graph.MAX_RESEARCH_UNITS


def test_agent_progress_events_are_emitted(client, big_kb, monkeypatch):
    """五 Agent 的运行痕迹要能到达前端（agent_status / artifact）。"""
    patch_model(monkeypatch)

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假怎么算？", "session_id": "s-prog", "category": "全部", "stream": True},
        headers=big_kb,
    )
    events = parse_sse(response.text)

    agents = [e.get("agent") for e in of_type(events, "agent_status")]
    for expected in ("Planner", "Researcher", "Analyst", "Writer", "Reviewer"):
        assert expected in agents, f"缺少 {expected} 的进度事件，实际 {set(agents)}"

    # 进度事件要带百分比，前端才能画进度条
    for event in of_type(events, "agent_status"):
        assert isinstance(event.get("percent"), int)

    # Planner 的拆解结果以 artifact 形式给出
    kinds = {e.get("kind") for e in of_type(events, "artifact")}
    assert "plan" in kinds, f"应有 plan artifact，实际 {kinds}"


def test_expression_only_revision_is_downgraded(client, big_kb, monkeypatch):
    """★ 只挑表达毛病的 revise 要降级为 pass —— 不改写、不重复正文。

    为什么重要（实测数据）：Reviewer 一次调用约 1.7 秒，Writer 重写约 1.9 秒。
    而重写发生在流式正文**已经推给用户之后**——重写的 token 会再流一遍，
    前端把它追加到已有正文后面，用户看到两份内容接在一起。
    更糟的是，若重写后第二轮评审撞上轮次上限，那次重写**根本没被校验**。
    """
    fake = patch_model(
        monkeypatch, FakeModel(script=FakeScript(units=2, verdict="revise"))
    )

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假怎么算？", "session_id": "s-down", "category": "全部", "stream": True},
        headers=big_kb,
    )
    events = parse_sse(response.text)

    verdicts = [e for e in of_type(events, "artifact") if e.get("kind") == "verdict"]
    assert verdicts, "应有 verdict artifact"
    assert verdicts[-1]["payload"]["downgraded"] is True, "表达类打回应被降级"

    # 关键：Writer 只跑一次（generate 节点只执行一轮）
    assert fake.calls == 1, f"Writer 不该重写，实际调用 {fake.calls} 次"
    assert not of_type(events, "reset"), "没有重写就不该发 reset"


def test_factual_issue_still_triggers_rewrite_and_reset(client, big_kb, monkeypatch):
    """★ 编造内容必须真的打回重写，且重写前要通知前端清空旧正文。

    ``reset`` 事件是必需的：delta 是增量语义，前端只会 append；
    不清空的话用户会看到"第一版 + 第二版"接在一起。
    """

    class FactualScript(FakeScript):
        def structured(self, schema):
            if schema.__name__ == "ReviewVerdict":
                self.reviews += 1
                # 第一轮判为编造 → 打回；第二轮放行（避免撞上限）
                if self.reviews == 1:
                    return schema(
                        verdict="revise",
                        notes="编造了资料中没有的技能名。",
                        evidence_gaps=[],
                        factual_issue=True,
                    )
                return schema(verdict="pass", notes="", evidence_gaps=[], factual_issue=False)
            return super().structured(schema)

    fake = patch_model(monkeypatch, FakeModel(script=FactualScript(units=2)))

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假怎么算？", "session_id": "s-fact", "category": "全部", "stream": True},
        headers=big_kb,
    )
    events = parse_sse(response.text)

    assert fake.calls == 2, f"编造应触发重写，实际 Writer 调用 {fake.calls} 次"
    resets = of_type(events, "reset")
    assert len(resets) == 1, "重写前必须发一次 reset"
    # reset 必须在第二次正文之前
    assert events.index(resets[0]) < max(
        i for i, e in enumerate(events) if e["type"] == "delta"
    )


def test_structured_output_never_leaks_into_delta(client, big_kb, monkeypatch):
    """结构化输出的 JSON 绝不能混进正文流。

    Planner/Researcher/Reviewer 的调用打了 ``nostream`` 标签，
    漏掉的话用户会在回答里看到 {"verdict": "pass"}。
    """
    patch_model(monkeypatch)

    response = client.post(
        "/api/v1/chat/chat",
        json={"question": "年假怎么算？", "session_id": "s-leak", "category": "全部", "stream": True},
        headers=big_kb,
    )
    body = "".join(e.get("content", "") for e in of_type(parse_sse(response.text), "delta"))
    for marker in ('"verdict"', '"unit_id"', '"evidence_gaps"', '"brief"'):
        assert marker not in body, f"结构化输出泄漏到正文：{marker}"


def test_graph_has_five_agent_nodes(monkeypatch):
    """图里必须有完整的五 Agent 节点。"""
    from app.services import rag_graph

    rag_graph.reset_graph()
    nodes = set(rag_graph.build_graph().get_graph().nodes.keys())
    for expected in (
        "classify",
        "retrieve",
        "planner",
        "researcher",
        "analyst",
        "generate",
        "reviewer",
        "no_evidence",
        "persist",
    ):
        assert expected in nodes, f"缺节点：{expected}"


# --------------------------------------------------------------------------- 检索可见性


def test_search_respects_private_visibility(client, user_headers, other_user_headers):
    """普通用户检索不到他人私有文档——过滤下推到 SQL，不是检索完再筛。"""
    from app.services import vector_store

    upload(client, user_headers, "alice私有.md", "年假需要提前三个工作日申请。")

    # bob 检索同一个问题，命中数必须是 0
    assert vector_store.search("年假提前几天", requester="bob", include_all=False, top_k=4) == []

    # alice 自己检索得到
    assert vector_store.search("年假提前几天", requester="alice", include_all=False, top_k=4)


def test_precheck_matches_search_visibility(client, user_headers, other_user_headers):
    """★ 预检的可见性条件必须与检索一致。

    两处各写一份的话，预检说"有资料"而检索返回空，
    用户会看到"资料不足"却不知道为什么。
    """
    from app.services import vector_store

    upload(client, user_headers, "alice私有.md", "年假需要提前三个工作日申请。")

    # bob 看不到 alice 的私有文档：预检与检索都要是 0
    assert vector_store.count_searchable(requester="bob", include_all=False) == 0
    assert vector_store.search("年假", requester="bob", include_all=False, top_k=4) == []

    # alice 自己：预检与检索都要有
    assert vector_store.count_searchable(requester="alice", include_all=False) > 0
    assert vector_store.search("年假", requester="alice", include_all=False, top_k=4)

    # root 覆盖他人私有文档
    assert vector_store.count_searchable(requester="admin", include_all=True) > 0


def test_admin_search_covers_private_documents(client, admin_headers, user_headers):
    """root 的检索覆盖他人私有文档——这是 root 语义，不是漏洞。"""
    from app.services import vector_store

    upload(client, user_headers, "alice私有.md", "年假需要提前三个工作日申请。")
    assert vector_store.search("年假提前几天", requester="admin", include_all=True, top_k=4)


def test_category_filter_is_applied(client, admin_headers, sample_text):
    from app.services import vector_store

    upload(client, admin_headers, "hr.md", sample_text, category="阵容攻略")
    upload(client, admin_headers, "it.md", "VPN 连不上时先检查账号是否过期。", category="技能图鉴")

    hr_hits = vector_store.search("年假", category="阵容攻略", include_all=True, top_k=4)
    assert all(hit["category"] == "阵容攻略" for hit in hr_hits)

    it_hits = vector_store.search("VPN", category="技能图鉴", include_all=True, top_k=4)
    assert all(hit["category"] == "技能图鉴" for hit in it_hits)

    # 预检也要遵守分类过滤，否则会走进五 Agent 再空手而归
    assert vector_store.count_searchable(category="技能图鉴", include_all=True) == 1
    assert vector_store.count_searchable(category="版本公告", include_all=True) == 0


def test_only_successful_documents_are_searchable(client, admin_headers, sample_text):
    """失败/处理中的文档不该被检索到。"""
    from app.services import storage, vector_store

    created = upload(client, admin_headers, "制度.md", sample_text).json()
    document_id = created["data"]["uploaded"][0]["document_id"]
    assert vector_store.search("年假", include_all=True, top_k=4)
    assert vector_store.count_searchable(include_all=True) > 0

    storage.update_document(document_id, {"status": "failed"})
    assert vector_store.search("年假", include_all=True, top_k=4) == []
    assert vector_store.count_searchable(include_all=True) == 0


# --------------------------------------------------------------------------- 会话归属


def test_history_is_owner_scoped(client, user_headers, other_user_headers):
    """普通用户读不到别人的会话历史。"""
    create_qa_log("alice-session", "alice")

    denied = client.get(
        "/api/v1/chat/history", params={"session_id": "alice-session"}, headers=other_user_headers
    )
    assert denied.json()["code"] == 403

    allowed = client.get(
        "/api/v1/chat/history", params={"session_id": "alice-session"}, headers=user_headers
    )
    assert allowed.json()["code"] == 200
    assert len(allowed.json()["data"]) == 2  # 一问一答


def test_admin_can_read_any_history(client, admin_headers, user_headers):
    create_qa_log("alice-s2", "alice")
    response = client.get(
        "/api/v1/chat/history", params={"session_id": "alice-s2"}, headers=admin_headers
    )
    assert response.json()["code"] == 200


def test_sessions_are_scoped_but_admin_sees_all(client, admin_headers, user_headers, other_user_headers):
    create_qa_log("alice-s", "alice")

    assert client.get("/api/v1/chat/sessions", headers=other_user_headers).json()["data"] == []

    alice_sessions = client.get("/api/v1/chat/sessions", headers=user_headers).json()["data"]
    assert len(alice_sessions) == 1
    assert alice_sessions[0]["session_id"] == "alice-s"
    assert alice_sessions[0]["turns"] == 1

    admin_sessions = client.get("/api/v1/chat/sessions", headers=admin_headers).json()["data"]
    assert len(admin_sessions) == 1


def test_delete_session_checks_ownership(client, user_headers, other_user_headers):
    create_qa_log("alice-del", "alice")

    denied = client.post(
        "/api/v1/chat/delete_session", json={"session_id": "alice-del"}, headers=other_user_headers
    )
    assert denied.json()["code"] != 200

    allowed = client.post(
        "/api/v1/chat/delete_session", json={"session_id": "alice-del"}, headers=user_headers
    )
    assert allowed.json()["code"] == 200


def test_admin_can_delete_any_session(client, admin_headers, user_headers):
    create_qa_log("alice-del2", "alice")
    response = client.post(
        "/api/v1/chat/delete_session", json={"session_id": "alice-del2"}, headers=admin_headers
    )
    assert response.json()["code"] == 200


def test_history_restores_source_docs(client, user_headers):
    """恢复历史会话时必须带回 source_docs，否则刷新后引用卡片就没了。"""
    create_qa_log(
        "alice-restore",
        "alice",
        source_docs=[
            {
                "document_id": "d1",
                "chunk_index": 2,
                "title": "制度",
                "category": "阵容攻略",
                "version": "v1.0",
                "file_name": "制度.md",
                "score": 0.9,
                "snippet": "片段",
            }
        ],
    )
    data = client.get(
        "/api/v1/chat/history", params={"session_id": "alice-restore"}, headers=user_headers
    ).json()["data"]
    assistant = [m for m in data if m["role"] == "assistant"][0]
    assert assistant["source_docs"][0]["chunk_index"] == 2


# --------------------------------------------------------------------------- 反馈


def test_feedback_upsert_is_idempotent(client, admin_headers):
    """同一问答重复评价只更新，不产生重复记录。"""
    from app.services import storage

    log = create_qa_log("s-fb", "admin")

    first = client.post(
        "/api/v1/chat/feedback",
        json={"qa_log_id": log["id"], "rating": "helpful", "comment": "", "session_id": "s-fb"},
        headers=admin_headers,
    )
    assert first.json()["code"] == 200

    second = client.post(
        "/api/v1/chat/feedback",
        json={
            "qa_log_id": log["id"],
            "rating": "needs_improvement",
            "comment": "改",
            "session_id": "s-fb",
        },
        headers=admin_headers,
    )
    assert second.json()["code"] == 200
    assert second.json()["data"]["rating"] == "needs_improvement"

    assert len(storage.list_feedback()) == 1


def test_feedback_rejects_unknown_rating(client, admin_headers):
    log = create_qa_log("s-fb2", "admin")
    response = client.post(
        "/api/v1/chat/feedback",
        json={"qa_log_id": log["id"], "rating": "amazing", "comment": "", "session_id": "s-fb2"},
        headers=admin_headers,
    )
    assert response.json()["code"] != 200


# --------------------------------------------------------------------------- 健康检查


def test_health_reports_degraded_for_hash_embeddings(client, admin_headers):
    """hash 伪嵌入必须被标成 degraded —— 否则用户会以为检索是准的。"""
    data = client.get("/api/v1/system/health", headers=admin_headers).json()["data"]
    assert data["embedding"]["provider"] == "hash"
    assert data["status"] == "degraded"
    assert "没有语义意义" in data["embedding"]["detail"]
