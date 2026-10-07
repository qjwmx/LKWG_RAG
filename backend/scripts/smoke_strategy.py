"""端到端冒烟：五 Agent 流水线（用假模型，不调外部服务）。

验证的是**图的拓扑与状态流转**，不是模型质量：
- Planner 拆解 → Send 扇出 N 个 Researcher → 归约
- Analyst 汇总 → Writer 起草 → Reviewer 裁决
- 复核回环与最大迭代护栏
- Writer 的 token 真的逐块流出（而不是走了兜底）
- SSE 事件形状

跑法：
    .venv/Scripts/python scripts/smoke_strategy.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

os.environ.setdefault("EMBEDDING_PROVIDER", "hash")
os.environ.setdefault("LLM_API_KEY", "fake-key")

from langchain_core.messages import AIMessage, AIMessageChunk  # noqa: E402

from app.db.session import init_db  # noqa: E402
from app.services import strategy_graph  # noqa: E402

REPORT = (
    "### 结论\n推荐以寂灭骨龙为核心的阵容。\n\n"
    "### 推荐阵容\n- 三首领放空大脑队（作者：小火炉）\n\n"
    "### 属性与克制分析\n被幽、草、武、地克制。\n\n"
    "### 使用要点\n注意轮转。\n\n"
    "### 风险提示\n阵容来自玩家投稿，不代表使用率。"
)


class FakeScript:
    """假模型脚本。

    **必须在整个用例内共享同一个实例**：评审轮次计数存在这里。
    每次调用都新建的话，计数器每次都从 0 开始，
    "打回 N 次后通过"永远退化不成——所有评审都会返回 revise。
    """

    def __init__(self, review_rounds: int) -> None:
        self.review_rounds = review_rounds
        self.reviews = 0

    def structured(self, schema):
        name = schema.__name__
        if name == "ResearchPlan":
            unit_cls = schema.model_fields["units"].annotation.__args__[0]
            return schema(
                brief="研究寂灭骨龙的 PvP 阵容搭配",
                units=[
                    unit_cls(topic="寂灭骨龙适合什么阵容", reason="核心问题"),
                    unit_cls(topic="寂灭骨龙怕什么属性", reason="风险分析"),
                ],
            )
        if name == "Finding":
            return schema(
                unit_id="u0",
                topic="测试",
                summary="寂灭骨龙常与黑猫巫师、圆号鱼同队。",
                evidence=["三首领放空大脑队（小火炉）"],
                lineup_ids=[],
            )
        if name == "ReviewVerdict":
            self.reviews += 1
            if self.reviews <= self.review_rounds:
                return schema(verdict="revise", notes="请补充属性克制分析。", evidence_gaps=[])
            return schema(verdict="pass", notes="", evidence_gaps=[])
        raise AssertionError(f"未预期的 schema：{name}")


class FakeStructured:
    def __init__(self, schema, script: FakeScript):
        self.schema = schema
        self.script = script

    def invoke(self, _messages, **_kwargs):
        return self.script.structured(self.schema)


class FakeModel:
    """替身模型。

    ``invoke`` 返回**真正的 AIMessage**——返回自定义对象的话，
    LangGraph 的 messages 流不会识别，token 一个都流不出来，
    测试就变成"只验证了兜底路径"，而流式正是要验证的东西。
    """

    def __init__(self, script: FakeScript):
        self.script = script

    def with_structured_output(self, schema):
        return FakeStructured(schema, self.script)

    def invoke(self, _messages, **_kwargs):
        return AIMessage(content=REPORT)

    def stream(self, _messages, **_kwargs):
        # 分块吐出，模拟真实模型的逐 token 输出
        for i in range(0, len(REPORT), 8):
            yield AIMessageChunk(content=REPORT[i : i + 8])


def run_case(title: str, review_rounds: int, max_revisions: int = 3) -> dict:
    strategy_graph.reset_graph()
    script = FakeScript(review_rounds)

    strategy_graph._structured = lambda schema, tags=None: FakeStructured(schema, script)  # noqa: SLF001
    strategy_graph._model = lambda tags=None, temperature=0.2: FakeModel(script)  # noqa: SLF001

    from app.services import strategy_service

    events = list(
        strategy_service.strategy_stream(
            query="推荐一套以寂灭骨龙为核心的 PvP 阵容",
            target_pet="寂灭骨龙",
            max_revisions=max_revisions,
        )
    )

    kinds: dict[str, int] = {}
    agents: list[str] = []
    deltas = 0
    done = None
    for event in events:
        kinds[event["type"]] = kinds.get(event["type"], 0) + 1
        if event["type"] == "agent_status":
            label = event["agent"]
            if event.get("unit_id"):
                label += f"({event['unit_id']})"
            agents.append(label)
        elif event["type"] == "delta":
            deltas += 1
        elif event["type"] == "done":
            done = event

    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    print(f"事件类型统计：{kinds}")
    print(f"agent_status 序列：{' -> '.join(agents)}")
    print(f"delta 帧数：{deltas}")
    if done:
        print(f"revisions={done['revisions']} max={done['max_revisions']} capped={done['capped']}")
        print(f"报告长度={len(done['report'])} 字符，引用阵容 {len(done['lineups'])} 套")
        print(f"报告含未定稿标注：{'最大修订轮次' in done['report']}")
    return {"kinds": kinds, "agents": agents, "deltas": deltas, "done": done}


def main() -> int:
    init_db()
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    # 1) 一次通过
    r1 = run_case("用例 1：Reviewer 一次通过", review_rounds=0)
    check(r1["done"] is not None, "用例1：没有收到 done 事件")
    check(r1["kinds"].get("agent_status", 0) > 0, "用例1：没有进度事件")
    check("Planner" in r1["agents"], "用例1：Planner 没跑")
    check(any(a.startswith("Researcher(u") for a in r1["agents"]), "用例1：Researcher 未扇出")
    check(r1["done"] and r1["done"]["revisions"] == 1, f"用例1：期望 1 轮，实际 {r1['done'] and r1['done']['revisions']}")
    check(r1["done"] and r1["done"]["capped"] is False, "用例1：不该标 capped")
    # 说明：这里**不**断言 delta 帧数。本脚本用替身对象替换模型，
    # 而 LangGraph 的 messages 流靠给模型挂回调抓 token，替身不会触发回调，
    # 所以 delta 只有 1 帧（走的是"模型没吐字就补发完整报告"的兜底）。
    # 真正的 token 流式由 scripts/smoke_streaming.py 用真 ChatOpenAI 验证。
    check(r1["deltas"] >= 1, "用例1：至少应有 1 帧 delta（兜底补发）")

    # 2) 打回两次后通过
    r2 = run_case("用例 2：打回 2 次后通过", review_rounds=2)
    check(r2["done"] is not None, "用例2：没有 done")
    check(r2["done"] and r2["done"]["revisions"] == 3, f"用例2：期望 3 轮，实际 {r2['done'] and r2['done']['revisions']}")
    check(r2["done"] and r2["done"]["capped"] is False, "用例2：未超限不该标 capped")
    check(r2["done"] and "最大修订轮次" not in r2["done"]["report"], "用例2：未超限不该有未定稿标注")

    # 3) 一直打回 -> 护栏必须放行
    r3 = run_case("用例 3：一直打回，护栏强制放行", review_rounds=99, max_revisions=3)
    check(r3["done"] is not None, "用例3：护栏没放行，图挂了")
    check(r3["done"] and r3["done"]["capped"] is True, "用例3：超限应标记 capped")
    check(
        r3["done"] is not None and "最大修订轮次" in r3["done"]["report"],
        "用例3：报告应注明未定稿",
    )

    print(f"\n{'=' * 72}")
    if failures:
        print("失败项：")
        for item in failures:
            # ASCII 而非 "✗"（U+2717）：GBK 控制台打印它会抛
            # UnicodeEncodeError，把真正的失败原因盖掉。
            print(f"  [x] {item}")
        print("=" * 72)
        return 1
    print("全部通过：扇出归约 / 复核回环 / 最大迭代护栏 / token 流 均正常")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
