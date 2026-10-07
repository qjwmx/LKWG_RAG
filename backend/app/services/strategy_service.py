"""攻略模式服务：把五 Agent 图的流式输出翻译成 SSE 事件。

事件契约（前端按 type 分发）：

```
{"type":"agent_status", "agent":"Researcher", "unit_id":"u0",
 "stage":"retrieving", "detail":"...", "percent":25}
{"type":"artifact", "agent":"Planner", "kind":"plan", "payload":{...}}
{"type":"delta", "content":"..."}          ← 只有 Writer 的正文
{"type":"done", "report":"...", "lineups":[...], "revisions":1, "degraded":false}
{"type":"error", "message":"..."}
```

三个关键实现点
--------------
1. **``version="v2"``**：统一帧形状为 ``{"type","ns","data"}``。
   不传的话，单个/多个 mode、是否含子图会给出四种不同形状
   （裸数据 / (mode,data) / (ns,data) / (ns,mode,data)），分发要写四套分支。

2. **``stream_mode=["updates","messages","custom"]``**：
   - ``custom`` —— 节点内 ``get_stream_writer()`` 发的富进度（agent_status/artifact）
   - ``messages`` —— 模型 token，**只取 Writer 的**（靠 ``langgraph_node`` 过滤）
   - ``updates`` —— 节点完成，用来判断是否已到终态

3. **Writer 的 token 过滤**：Planner/Analyst/Reviewer 的结构化输出已打
   ``nostream`` 标签不会出现；但为稳妥再按 ``langgraph_node == "writer"``
   过滤一次。**两层保险**，因为一旦漏掉，前端会看到 JSON 混在正文里。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator

from app.services import checkpoint, roco_store, strategy_graph, web_search

logger = logging.getLogger(__name__)


def _render_lineups(lineup_ids: list[str]) -> list[dict]:
    """把阵容 id 补成完整阵容（含成员），供前端渲染卡片。"""
    result: list[dict] = []
    seen: set[str] = set()
    for lineup_id in lineup_ids:
        if not lineup_id or lineup_id in seen:
            continue
        seen.add(lineup_id)
        lineup = roco_store.get_lineup(lineup_id)
        if lineup:
            result.append(lineup)
    return result


def strategy_stream(
    query: str,
    target_pet: str = "",
    lineup_type: str = "pvp",
    max_revisions: int = strategy_graph.DEFAULT_MAX_REVISIONS,
    use_web: bool = False,
    username: str = "",
    session_id: str = "",
) -> Iterator[dict]:
    """跑五 Agent 流水线，逐事件产出。

    ``session_id`` 决定多轮上下文归属。真实 thread_id 是
    ``"{username}:{session_id}"``（见 ``checkpoint.thread_id_for``）——
    **必须带用户名前缀**，否则用户 A 传 B 的 session_id 就能读到 B 的追问上下文。
    """
    graph = strategy_graph.build_graph()
    state = strategy_graph.initial_state(
        query=query,
        target_pet=target_pet,
        lineup_type=lineup_type,
        max_revisions=max_revisions,
        use_web=use_web,
    )

    final: dict = {}
    emitted_any = False

    # 联网不可用时**提前告知**，不要等到 done 才说。
    # 用户打开开关却什么都没发生，会以为开关坏了。
    if use_web and not web_search.is_configured():
        yield {"type": "web_unavailable", "message": web_search.UNAVAILABLE_MESSAGE}

    try:
        for part in graph.stream(
            state,
            # **必须同时订阅 values**：``updates`` 给出的是每个节点**原始返回**的
            # 增量，不是归约后的状态。并行 Researcher 各自返回
            # ``{"findings": [一条]}``，如果只靠 updates 累加，
            # 后完成的会覆盖先完成的，最终只剩一条——而且不报错。
            # ``values`` 给出每步之后的**完整归约状态**，取最后一次快照即最终态。
            stream_mode=["updates", "messages", "custom", "values"],
            version="v2",
            config={
                # 递归上限给足：复核回环最多 3 轮，加上节点本身，
                # 50 足够且能兜住任何意外的无限循环。
                "recursion_limit": 50,
                # **挂了 checkpointer 就必须传 thread_id**，
                # 否则 LangGraph 直接抛 ValueError（不是静默无状态）。
                "configurable": {"thread_id": checkpoint.thread_id_for(username, session_id)},
            },
        ):
            kind = part.get("type")
            data = part.get("data")

            if kind == "custom":
                # 节点内直接发的进度事件，原样转发
                if isinstance(data, dict) and data.get("type"):
                    yield data
                continue

            if kind == "messages":
                chunk, meta = data
                # 只推 Writer 的 token。结构化输出的调用已打 nostream，
                # 这里是第二层保险。
                if meta.get("langgraph_node") != "writer":
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, list):
                    content = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                    )
                if not content:
                    continue
                emitted_any = True
                yield {"type": "delta", "content": content}
                continue

            if kind == "values":
                # 完整归约状态：直接整体替换，不做增量合并
                if isinstance(data, dict):
                    final = data
                continue

            if kind == "updates":
                if isinstance(data, dict) and "__interrupt__" in data:
                    # 本图没有用 interrupt，出现即异常
                    logger.warning("意外的 interrupt：%s", data["__interrupt__"])
                continue
    except Exception as exc:  # noqa: BLE001 - 流已开始，改不了状态码
        logger.exception("攻略图执行失败（query=%s）", query)
        yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
        return

    if final.get("error"):
        yield {"type": "error", "message": final["error"]}
        return

    report = final.get("report") or final.get("draft") or ""

    # 模型可能一个字都没吐（例如直接把结构化结果当正文），
    # 那前端会是一片空白——补发一次完整报告。
    if not emitted_any and report:
        yield {"type": "delta", "content": report}

    # 收集引用到的阵容，补成完整卡片
    lineup_ids: list[str] = []
    for finding in final.get("findings") or []:
        lineup_ids.extend(finding.get("lineup_ids") or [])

    # 兜底：模型可能忘了填 lineup_ids（schema 里它是可选字段），
    # 但 Researcher 明明检索到了阵容。这时**把检索到的阵容也带上**——
    # 报告的结论就是基于它们得出的，不展示出来等于让用户无法核对依据。
    # 宁可多给几张卡片，也不要让报告看起来"没有出处"。
    if not lineup_ids:
        for finding in final.get("findings") or []:
            lineup_ids.extend(_lineups_mentioned_in(finding))

    yield {
        "type": "done",
        "report": report,
        "lineups": _render_lineups(lineup_ids),
        "revisions": final.get("revision_count") or 0,
        "max_revisions": final.get("max_revisions") or max_revisions,
        # 超限放行过就要如实标注，不能让用户以为这是定稿
        "capped": (final.get("revision_count") or 0) > (
            final.get("max_revisions") or max_revisions
        ),
        "units": final.get("units") or [],
        "findings": final.get("findings") or [],
        "gaps": final.get("evidence_gaps") or [],
        # 联网出处要带给前端：报告里写了「网络来源」却点不进去核对，
        # 等于让用户无法验证。去重实现提到 web_search，两模式共用。
        "web_sources": web_search.dedupe(final.get("web_results") or []),
        # 联网失败不是致命错误，但**必须如实告知**，否则用户会以为
        # "开了联网但网上什么都没有"，而其实是没查成。
        "web_error": final.get("web_error") or "",
        "session_id": session_id,
    }


def _lineups_mentioned_in(finding: dict) -> list[str]:
    """从一条发现的证据文本里反查它提到了哪些阵容。

    用「库里已有的阵容标题出现在文本里」来判断——标题是专有名词，
    精确匹配一定能中，不需要分词。
    """
    text = (finding.get("summary") or "") + " ".join(finding.get("evidence") or [])
    if not text:
        return []
    matched: list[str] = []
    for lineup in roco_store.list_lineups(lineup_type="全部", limit=500):
        if lineup["title"] and lineup["title"] in text:
            matched.append(lineup["id"])
    return matched


def sse_frame(event: dict) -> str:
    """编码成 SSE 帧。与对话模式共用同一套帧格式。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def run_strategy(
    query: str,
    target_pet: str = "",
    lineup_type: str = "pvp",
    max_revisions: int = strategy_graph.DEFAULT_MAX_REVISIONS,
    use_web: bool = False,
    username: str = "",
    session_id: str = "",
) -> dict:
    """非流式跑一遍（测试与脚本用）。"""
    graph = strategy_graph.build_graph()
    final = graph.invoke(
        strategy_graph.initial_state(
            query=query,
            target_pet=target_pet,
            lineup_type=lineup_type,
            max_revisions=max_revisions,
            use_web=use_web,
        ),
        config={
            "recursion_limit": 50,
            "configurable": {"thread_id": checkpoint.thread_id_for(username, session_id)},
        },
    )
    if final.get("error"):
        raise RuntimeError(final["error"])
    return final
