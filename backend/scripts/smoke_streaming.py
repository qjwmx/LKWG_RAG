"""集成冒烟：用**真实** ChatOpenAI（指向本地假模型服务）验证 token 流式。

为什么需要这个单独的脚本
------------------------
``smoke_strategy.py`` 用替身对象替换模型，能验证图的拓扑、归约、护栏，
但**验证不了 token 流式**：LangGraph 的 ``stream_mode="messages"`` 靠给模型挂
回调来抓 token，而替身对象不会触发那些回调。所以那个脚本里
``delta`` 只有 1 帧（走的兜底补发），这**不代表产品有问题**。

要真验证流式，必须用真的 ``ChatOpenAI`` 打一个真的 HTTP 端点。
这里用 ``scripts/fake_llm_server.py``（OpenAI 兼容、会逐块吐字）。

分工：
- 结构化输出（Planner/Analyst/Reviewer）→ 替身（假服务器不返回 JSON）
- Writer 的正文 → **真的 ChatOpenAI** → 走完整流式链路

跑法（先起假模型）：
    .venv/Scripts/python scripts/fake_llm_server.py          # 终端 1
    .venv/Scripts/python scripts/smoke_streaming.py          # 终端 2
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

FAKE_LLM = os.environ.get("FAKE_LLM_BASE", "http://127.0.0.1:8021/v1")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")
os.environ["LLM_BASE_URL"] = FAKE_LLM
os.environ["LLM_API_KEY"] = "fake-key"
os.environ["LLM_MODEL"] = "fake"

from app.db.session import init_db  # noqa: E402
from app.services import strategy_graph  # noqa: E402


class FakeScript:
    def __init__(self) -> None:
        self.reviews = 0

    def structured(self, schema):
        name = schema.__name__
        if name == "ResearchPlan":
            unit_cls = schema.model_fields["units"].annotation.__args__[0]
            return schema(
                brief="研究寂灭骨龙的 PvP 阵容搭配",
                units=[unit_cls(topic="寂灭骨龙适合什么阵容", reason="核心问题")],
            )
        if name == "Finding":
            return schema(
                unit_id="u0",
                topic="测试",
                summary="寂灭骨龙常与黑猫巫师同队。",
                evidence=["三首领放空大脑队"],
                lineup_ids=[],
            )
        if name == "ReviewVerdict":
            self.reviews += 1
            return schema(verdict="pass", notes="", evidence_gaps=[])
        raise AssertionError(f"未预期的 schema：{name}")


class FakeStructured:
    def __init__(self, schema, script):
        self.schema = schema
        self.script = script

    def invoke(self, _messages, **_kwargs):
        return self.script.structured(self.schema)


def main() -> int:
    init_db()

    # 先确认假模型活着——否则失败原因会被误读成"流式坏了"
    import httpx

    try:
        httpx.post(
            f"{FAKE_LLM}/chat/completions",
            json={"model": "fake", "messages": [{"role": "user", "content": "ping"}]},
            timeout=10.0,
            trust_env=False,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"假模型服务不可达（{FAKE_LLM}）：{type(exc).__name__}: {exc}")
        print("请先运行：.venv/Scripts/python scripts/fake_llm_server.py")
        return 2

    strategy_graph.reset_graph()
    script = FakeScript()
    # 只替换结构化输出；Writer 走真模型
    strategy_graph._structured = lambda schema, tags=None: FakeStructured(schema, script)  # noqa: SLF001

    from app.services import strategy_service

    deltas: list[str] = []
    done = None
    agents: list[str] = []

    for event in strategy_service.strategy_stream(
        query="推荐一套以寂灭骨龙为核心的 PvP 阵容",
        target_pet="寂灭骨龙",
    ):
        if event["type"] == "delta":
            deltas.append(event["content"])
        elif event["type"] == "agent_status":
            agents.append(event["agent"])
        elif event["type"] == "done":
            done = event
        elif event["type"] == "error":
            print(f"ERROR: {event['message']}")
            return 1

    text = "".join(deltas)
    print("=" * 72)
    print(f"agent 序列：{' -> '.join(agents)}")
    print(f"delta 帧数：{len(deltas)}")
    print(f"拼接正文长度：{len(text)} 字符")
    print(f"正文前 80 字：{text[:80]!r}")
    print(f"done 事件：report={len(done['report']) if done else 0} 字符")
    print("=" * 72)

    failures = []
    # 真流式的判据：帧数应该远多于 1（每块 6 字符，正文约 300 字符 -> 约 50 帧）
    if len(deltas) <= 3:
        failures.append(f"delta 只有 {len(deltas)} 帧，token 流没生效")
    if "寂灭骨龙" not in text and "年假" not in text:
        failures.append("拼接出的正文不像模型输出")
    if done is None:
        failures.append("没有 done 事件")

    if failures:
        print("失败项：")
        for item in failures:
            # 用 ASCII 的 [x] 而不是 "✗"（U+2717）：Windows 控制台默认 GBK，
            # 打印它会抛 UnicodeEncodeError —— 而且是在**打印失败清单时**抛，
            # 真正的失败原因被 traceback 盖掉。冒烟脚本尤其不能这样。
            print(f"  [x] {item}")
        return 1

    print(f"[OK] 真实 token 流式生效：{len(deltas)} 个 delta 帧")
    return 0


if __name__ == "__main__":
    sys.exit(main())
