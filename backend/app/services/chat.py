"""问答服务：把 LangGraph 五 Agent 流水线的流式输出转成 SSE 事件。

这一层只做**协议转换**，不承载业务逻辑——拆解、检索、交叉验证、撰写、复核
都在 ``rag_graph`` 的图里。这样"图怎么走"和"帧怎么发"可以各自独立改动。

事件契约（前端按 type 分发）：

```
{"type":"agent_status", "agent":"Researcher", "unit_id":"u0",
 "stage":"retrieving", "detail":"...", "percent":25}
{"type":"artifact", "agent":"Planner", "kind":"plan", "payload":{...}}
{"type":"delta", "content":"..."}          ← 只有 Writer 的正文
{"type":"web_unavailable", "message":"..."} ← 开了联网但服务端没配
{"type":"done", ...}                        ← qa_log_id / source_docs / answer / web_sources
{"type":"error", "message":"..."}
```

时序要点：``qa_logs`` 必须等正文流完才能写（要落完整 answer），
因此 ``done`` 事件一定在 ``persist`` 节点执行之后产出。
客户端中途断开时这一轮不会落库。

**前端兼容**：``agent_status`` / ``artifact`` 是攻略模式已有的帧类型，
前端 store 里只处理 ``delta`` / ``done`` / ``error``，未知类型会被忽略——
所以新增进度帧不会打破现有对话界面。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from app.services import rag_graph, web_search

logger = logging.getLogger(__name__)


def answer_question(
    question: str,
    session_id: str,
    category: str = "全部",
    username: str = "",
    is_admin: bool = False,
    use_web: bool = False,
) -> dict:
    """非流式问答。直接跑完图再取最终状态。"""
    graph = rag_graph.build_graph()
    final = graph.invoke(
        rag_graph.initial_state(question, session_id, category, username, is_admin, use_web=use_web),
        config={"recursion_limit": 50},
    )

    if final.get("error"):
        raise RuntimeError(final["error"])

    return {
        "answer": final.get("answer") or "",
        "question_type": final.get("question_type") or "",
        "qa_log_id": final.get("qa_log_id") or "",
        "source_docs": final.get("references") or [],
        "web_sources": web_search.dedupe(final.get("web_results") or []),
        "web_error": final.get("web_error") or "",
    }


def answer_question_stream(
    question: str,
    session_id: str,
    category: str = "全部",
    username: str = "",
    is_admin: bool = False,
    use_web: bool = False,
) -> Iterator[dict]:
    """逐事件产出，供 SSE 使用。

    用 ``stream_mode=["updates","messages","custom","values"]`` + ``version="v2"``：

    - ``custom`` —— 节点内 ``get_stream_writer()`` 发的富进度（agent_status/artifact）
    - ``messages`` —— 模型 token，**只取 generate 节点的**（靠 ``langgraph_node`` 过滤）
    - ``values`` —— 每步之后的**完整归约状态**，取最后一次快照即最终态

    ``version="v2"`` 统一帧形状为 ``{"type","ns","data"}``。不传的话，
    单个/多个 mode、是否含子图会给出四种不同形状，分发要写四套分支。

    **必须订阅 values 而不是只靠 updates 累加**：并行 Researcher 各自返回
    ``{"findings":[一条]}``，只累加 updates 时后完成的会覆盖先完成的。
    """
    graph = rag_graph.build_graph()
    state = rag_graph.initial_state(
        question, session_id, category, username, is_admin, use_web=use_web
    )

    final_state: dict = {}
    emitted_any = False
    # Writer 跑了几次。>1 说明发生过重写，此时它的 token 会**重新**流一遍；
    # 不通知前端清空的话，用户会看到"第一版 + 第二版"接在一起。
    generate_rounds = 0

    # 联网不可用时**提前告知**，不要等到 done 才说。
    # 用户打开开关却什么都没发生，会以为开关坏了。
    if use_web and not web_search.is_configured():
        yield {"type": "web_unavailable", "message": web_search.UNAVAILABLE_MESSAGE}

    try:
        for part in graph.stream(
            state,
            stream_mode=["updates", "messages", "custom", "values"],
            version="v2",
            # 递归上限给足：复核回环最多 1 轮，加上节点本身，
            # 50 足够且能兜住任何意外的无限循环。
            config={"recursion_limit": 50},
        ):
            kind = part.get("type")
            data = part.get("data")

            if kind == "custom":
                # 节点内直接发的进度事件，原样转发
                if isinstance(data, dict) and data.get("type"):
                    yield data
                continue

            if kind == "updates":
                # 用 updates 数 Writer 跑了几轮。它给的是节点**原始返回**，
                # 但这里只需要"generate 节点是否又执行了一次"这个信号，
                # 不需要归约——正文与引用都取自 values 的最终快照。
                if isinstance(data, dict):
                    if "__interrupt__" in data:
                        # 本图没有用 interrupt，出现即异常
                        logger.warning("意外的 interrupt：%s", data["__interrupt__"])
                    elif "generate" in data:
                        generate_rounds += 1
                        if generate_rounds > 1:
                            # 重写开始：让前端丢掉上一版正文再重新累积。
                            # 这一步是必须的——delta 是增量语义，前端只会 append。
                            yield {"type": "reset"}
                continue

            if kind == "messages":
                chunk, meta = data
                # 只推 generate 的 token。结构化输出的调用已打 nostream，
                # 这里是第二层保险——一旦漏掉，前端会看到 JSON 混在正文里。
                if meta.get("langgraph_node") != "generate":
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, list):
                    content = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                    )
                # 跳过空片段：流里会夹带大量 content="" 的 chunk，
                # 不过滤的话一次回答能产生上千个无用 SSE 帧。
                if not content:
                    continue
                emitted_any = True
                yield {"type": "delta", "content": content}
                continue

            if kind == "values":
                # 完整归约状态：直接整体替换，不做增量合并
                if isinstance(data, dict):
                    final_state = data
                continue
    except Exception as exc:  # noqa: BLE001 - 流已开始，无法再改 HTTP 状态码
        logger.exception("问答图执行失败（session_id=%s）", session_id)
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
        return

    if final_state.get("error"):
        # 图内部已经给出可读错误（模型不可用 / 落库失败）
        yield {"type": "error", "message": final_state["error"]}
        return

    answer = final_state.get("answer") or ""
    references = final_state.get("references") or []

    # 无依据路径不发 delta（图里只是把固定文案写进 answer），
    # 这里补发一次，前端才能显示完整提示。
    if not emitted_any and answer:
        yield {"type": "delta", "content": answer}

    yield {
        "type": "done",
        "qa_log_id": final_state.get("qa_log_id") or "",
        "question_type": final_state.get("question_type") or "",
        "source_docs": references,
        "answer": answer,
        # 正文是逐段吐出的、不含引用段落，这里把渲染好的引用段落一并给出，
        # 免得前端再复制一份格式化逻辑
        "references_markdown": rag_graph._format_reference_markdown(references),
        # 联网出处（已按 url 去重）。空数组 = 没联网或网上没找到。
        "web_sources": web_search.dedupe(final_state.get("web_results") or []),
        # 联网失败不是致命错误，回答已经产出了，只是要如实告知没联上网
        "web_error": final_state.get("web_error") or "",
        # 五 Agent 的运行痕迹，供前端展示检索过程
        "units": final_state.get("units") or [],
        "findings": final_state.get("findings") or [],
        "revisions": final_state.get("revision_count") or 0,
    }


def sse_frame(event: dict) -> str:
    """把一个事件编码成 SSE 帧。

    只用 ``data:`` 字段，不用 SSE 的 ``event:``——客户端一次 ``json.loads``
    即可，不必再实现一套事件名分发。
    """
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def infer_question_type(question: str) -> str:
    """兼容旧调用点：题型推断现在在图里做，这里转发。"""
    return rag_graph.classify({"question": question})["question_type"]


# 保留模块级引用，方便测试替换
_chat_model_factory = rag_graph.get_chat_model
