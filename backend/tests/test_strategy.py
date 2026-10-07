"""五 Agent 流水线的测试（替身模型，不依赖外部服务）。

验证四件事，每一件都对应一个**容易被写错且症状隐蔽**的地方：

1. **Send 扇出真的发生了** —— 没接对的话图能跑完，但只有一次研究、
   topic 还是空的（表现为"检索不到东西"而不是报错）。
2. **归约没丢结果** —— ``findings`` 少了 ``operator.add`` 时，
   并行研究的结果会互相覆盖，最后只剩一条。
3. **复核回环方向正确** —— 证据不足回 Researcher、表达问题回 Writer。
   混成一个方向会让文字问题触发无谓的重新检索。
4. **护栏强制放行** —— 超限时返回 END 而不是撞 recursion_limit，
   且报告里注明未定稿。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

from app.services import strategy_graph

REPORT = "### 结论\n测试报告内容。\n\n### 风险提示\n阵容来自玩家投稿。"


class FakeScript:
    """假模型脚本。评审计数在实例内共享，否则"打回 N 次"退化不成。

    ``seen`` 记录每次模型调用收到的消息列表——提示注入的防线
    （``<web_content>`` 隔离、``_WEB_GUARD`` 声明）**只能靠检查实际
    送进模型的内容来验证**，光看代码是看不出漏没漏的。
    """

    def __init__(self, review_rounds: int = 0, units: int = 2, gaps: list[str] | None = None):
        self.review_rounds = review_rounds
        self.units = units
        self.gaps = gaps or []
        self.reviews = 0
        self.plan_calls = 0
        self.research_calls = 0
        self.seen: list[list] = []

    def _record(self, messages) -> None:
        self.seen.append(list(messages))

    def all_text(self) -> str:
        """把所有送进模型的文本拼起来，便于断言"某段内容有没有进去"。"""
        parts: list[str] = []
        for messages in self.seen:
            for item in messages:
                if isinstance(item, tuple):
                    parts.append(str(item[1]))
                else:
                    parts.append(getattr(item, "content", str(item)))
        return "\n".join(parts)

    def structured(self, schema):
        name = schema.__name__
        if name == "ResearchPlan":
            self.plan_calls += 1
            unit_cls = schema.model_fields["units"].annotation.__args__[0]
            return schema(
                brief="测试研究简报",
                units=[
                    unit_cls(topic=f"子问题{i}", reason=f"理由{i}")
                    for i in range(self.units)
                ],
            )
        if name == "Finding":
            self.research_calls += 1
            return schema(
                unit_id="u0",
                topic="子问题",
                summary="检索到的结论",
                evidence=["某阵容（作者：某人）"],
                lineup_ids=[],
            )
        if name == "ReviewVerdict":
            self.reviews += 1
            if self.reviews <= self.review_rounds:
                return schema(
                    verdict="revise",
                    notes="请补充说明。",
                    evidence_gaps=self.gaps if self.reviews == 1 else [],
                )
            return schema(verdict="pass", notes="", evidence_gaps=[])
        raise AssertionError(f"未预期的 schema：{name}")


class FakeStructured:
    def __init__(self, schema, script: FakeScript):
        self.schema = schema
        self.script = script

    def invoke(self, messages, **_kwargs):
        self.script._record(messages)
        return self.script.structured(self.schema)


class FakeModel:
    """``invoke`` 返回真正的 AIMessage。

    返回自定义对象的话 LangGraph 的 messages 流识别不了，
    测试就只验证了兜底路径。
    """

    def __init__(self, script: FakeScript):
        self.script = script

    def with_structured_output(self, schema, **_kwargs):
        return FakeStructured(schema, self.script)

    def invoke(self, messages, **_kwargs):
        self.script._record(messages)
        return AIMessage(content=REPORT)

    def stream(self, messages, **_kwargs):
        self.script._record(messages)
        for i in range(0, len(REPORT), 8):
            yield AIMessageChunk(content=REPORT[i : i + 8])


@pytest.fixture
def patch_graph(monkeypatch):
    """把图里的模型换成替身，返回脚本实例以便断言。"""

    def _patch(**kwargs) -> FakeScript:
        strategy_graph.reset_graph()
        script = FakeScript(**kwargs)
        monkeypatch.setattr(
            strategy_graph, "_structured", lambda schema, tags=None: FakeStructured(schema, script)
        )
        monkeypatch.setattr(
            strategy_graph, "_model", lambda tags=None, temperature=0.2: FakeModel(script)
        )
        return script

    yield _patch
    strategy_graph.reset_graph()


def run(query: str = "推荐一套阵容", **kwargs):
    """跑一轮流水线。

    ``session_id`` / ``username`` 决定 checkpoint 线程归属。不传的话
    ``checkpoint.thread_id_for`` 会生成**随机** session_id，
    即每轮都是全新线程（等价于无状态）——这正是绝大多数用例想要的。
    验证多轮追问的用例显式传固定 ``session_id``。
    """
    from app.services import strategy_service

    return list(
        strategy_service.strategy_stream(
            query=query,
            target_pet=kwargs.pop("target_pet", "寂灭骨龙"),
            max_revisions=kwargs.pop("max_revisions", 3),
            **kwargs,
        )
    )


def collect(events: list[dict]) -> dict:
    done = None
    statuses = []
    deltas = []
    errors = []
    for event in events:
        if event["type"] == "agent_status":
            statuses.append(event)
        elif event["type"] == "delta":
            deltas.append(event["content"])
        elif event["type"] == "done":
            done = event
        elif event["type"] == "error":
            errors.append(event["message"])
    return {"done": done, "statuses": statuses, "deltas": deltas, "errors": errors}


# --------------------------------------------------------------------------- 扇出


def test_planner_fans_out_one_researcher_per_unit(patch_graph, seeded_domain):
    """★ Send 扇出必须真的发生。

    没接对的话 planner 会直接连到 researcher，带完整 state 跑一次
    （topic 为空），Send 永远不触发——图能跑完，但只有一次研究。

    依赖 ``seeded_domain``：Researcher 会先查本地库，**库空时会早退、
    根本不调用模型**，于是 ``research_calls`` 恒为 0。不声明这个依赖的话，
    单独跑本文件会失败，而失败信息看起来像"扇出坏了"。
    """
    script = patch_graph(units=3)
    result = collect(run())

    assert script.plan_calls == 1, "Planner 应只跑一次"
    # 每个研究单元一个 Researcher
    assert script.research_calls == 3, f"应有 3 次研究，实际 {script.research_calls}"

    researcher_units = [
        s.get("unit_id") for s in result["statuses"] if s["agent"] == "Researcher" and s.get("unit_id")
    ]
    assert len(set(researcher_units)) == 3, f"应有 3 个不同单元，实际 {set(researcher_units)}"


def test_findings_are_reduced_not_overwritten(patch_graph):
    """★ 归约：并行的多条发现都要保留。

    ``findings`` 少了 ``operator.add`` 时，后完成的研究会覆盖先完成的，
    最终只剩一条——而且不报错。
    """
    patch_graph(units=3)
    result = collect(run())
    done = result["done"]
    assert done is not None
    assert len(done["findings"]) == 3, f"3 个单元应产出 3 条发现，实际 {len(done['findings'])}"


def test_units_are_capped(patch_graph):
    """研究单元数量有上限，避免扇出过多拖长耗时。"""
    script = patch_graph(units=10)
    result = collect(run())
    done = result["done"]
    assert done is not None
    assert len(done["units"]) <= strategy_graph.MAX_RESEARCH_UNITS
    assert script.research_calls <= strategy_graph.MAX_RESEARCH_UNITS


# --------------------------------------------------------------------------- 复核回环与护栏


def test_pass_on_first_review(patch_graph):
    script = patch_graph(review_rounds=0)
    result = collect(run())
    assert result["done"]["revisions"] == 1
    assert result["done"]["capped"] is False
    assert script.reviews == 1, "通过时应只审一次"


def test_revision_loop_then_pass(patch_graph):
    """打回后重写，再通过。"""
    script = patch_graph(review_rounds=2)
    result = collect(run(max_revisions=5))
    assert result["done"]["revisions"] == 3
    assert result["done"]["capped"] is False
    assert "最大修订轮次" not in result["done"]["report"], "未超限不该有未定稿标注"


def test_guardrail_forces_release_and_marks_report(patch_graph):
    """★ 护栏：一直打回时**必须放行**，而不是撞 recursion_limit 崩掉。

    用户永远拿到产出；报告里注明未定稿，不假装完美。
    """
    patch_graph(review_rounds=99)
    result = collect(run(max_revisions=3))

    assert result["done"] is not None, "护栏没放行，整轮失败了"
    assert result["errors"] == [], f"不该有错误：{result['errors']}"
    assert result["done"]["capped"] is True, "超限应标记 capped"
    assert "最大修订轮次" in result["done"]["report"], "报告应注明未定稿"


def test_evidence_gaps_route_back_to_researcher(patch_graph, seeded_domain):
    """★ 回环方向：证据不足要回 Researcher 补研究，而不是让 Writer 重写。

    方向搞错的话，Writer 会反复改写同一批不足的材料，永远改不好，
    直到撞上轮次上限。

    同样依赖 ``seeded_domain``（库空时 Researcher 早退、不调模型）。
    """
    script = patch_graph(review_rounds=1, gaps=["缺少属性克制数据"])
    result = collect(run(max_revisions=5))

    # 补研究会产生额外的 Researcher 调用（初次扇出 + 补证）
    assert script.research_calls >= 2, "证据缺口应触发补研究"
    assert result["done"] is not None


# --------------------------------------------------------------------------- 流式与错误


def test_writer_tokens_stream(patch_graph):
    """Writer 的正文要逐块流出（替身模型走兜底补发，至少 1 帧）。"""
    patch_graph()
    result = collect(run())
    assert result["deltas"], "应有 delta 帧"
    assert "测试报告内容" in "".join(result["deltas"]) or result["done"]["report"]


def test_planner_failure_is_reported_not_crashed(patch_graph, monkeypatch):
    """Planner 失败要转成可读错误，而不是抛异常崩掉整轮。"""

    class Boom(FakeScript):
        def structured(self, schema):
            if schema.__name__ == "ResearchPlan":
                raise RuntimeError("模拟模型不可用")
            return super().structured(schema)

    strategy_graph.reset_graph()
    script = Boom()
    monkeypatch.setattr(
        strategy_graph, "_structured", lambda schema, tags=None: FakeStructured(schema, script)
    )
    monkeypatch.setattr(
        strategy_graph, "_model", lambda tags=None, temperature=0.2: FakeModel(script)
    )

    result = collect(run())
    assert result["errors"], "应报出错误"
    assert "模拟模型不可用" in result["errors"][0]
    assert result["done"] is None, "失败时不该有 done 事件"


def test_graph_has_expected_nodes(patch_graph):
    patch_graph()
    graph = strategy_graph.build_graph()
    nodes = set(graph.get_graph().nodes.keys())
    for expected in ("planner", "researcher", "analyst", "writer", "reviewer", "finalize"):
        assert expected in nodes, f"缺节点：{expected}"


# --------------------------------------------------------------------------- 结构化输出为 None


class NoneStructured:
    """``with_structured_output`` 返回 ``None`` 的替身。

    这不是臆想的场景：**实测**上游偶发不返回 tool_calls 时，
    LangChain 的 ``with_structured_output`` 会返回 ``None`` 而**不抛异常**。
    真实事故：``strategy_graph.researcher`` 里 ``finding.summary`` 直接
    AttributeError，整个请求 500。
    """

    def __init__(self, schema):
        self.schema = schema

    def invoke(self, _messages, **_kwargs):
        return None


def test_structured_output_none_does_not_crash(patch_graph, monkeypatch):
    """★ 结构化输出返回 None 时必须优雅降级，不能 500。

    真实事故：上游偶发不返回 tool_calls -> ``finding.summary`` 抛
    ``AttributeError: 'NoneType' object has no attribute 'summary'``，
    整轮研究直接 500。一个单元的结构化失败不该让整个请求崩掉。

    这里让**所有**结构化调用都返回 None，验证整张图仍然跑完并给出结果。
    """
    strategy_graph.reset_graph()
    monkeypatch.setattr(
        strategy_graph, "_structured", lambda schema, tags=None: NoneStructured(schema)
    )
    script = FakeScript()
    monkeypatch.setattr(
        strategy_graph, "_model", lambda tags=None, temperature=0.2: FakeModel(script)
    )

    result = collect(run())

    assert result["errors"] == [], f"不该报错，实际：{result['errors']}"
    assert result["done"] is not None, "仍应产出 done 事件（降级而不是崩掉）"
    # Planner 退化成单单元、Researcher 降级为"资料不足"、Reviewer 放行
    assert result["done"].get("units"), "Planner 应退化成单单元"
    assert result["done"].get("report"), "仍应产出报告"


def test_rag_graph_structured_output_none_does_not_crash(monkeypatch):
    """对话图同样要扛住 None——它是主功能，更不能 500。"""
    from app.services import rag_graph

    rag_graph.reset_graph()
    monkeypatch.setattr(rag_graph, "_structured", lambda schema: NoneStructured(schema))

    graph = rag_graph.build_graph()
    final = graph.invoke(
        rag_graph.initial_state("年假怎么申请", "s-none-guard", "全部", "admin", True),
        config={"recursion_limit": 50},
    )

    assert not final.get("error"), f"不该报错，实际：{final.get('error')}"
    assert final.get("answer"), "仍应给出答案（降级而不是崩掉）"
    rag_graph.reset_graph()
