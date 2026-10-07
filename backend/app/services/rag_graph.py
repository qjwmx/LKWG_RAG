"""对话问答的五 Agent 检索流水线（LangChain + LangGraph）。

与攻略模式的关系
----------------
攻略模式（``strategy_graph``）问的是「这套阵容怎么打」，证据来自**结构化表**
（精灵、阵容、克制矩阵）。对话模式问的是「这个机制是什么」，证据来自
**向量库**——用户上传并切分入库的攻略文档。两者的取证手段不同，但
「拆解 → 并行取证 → 交叉验证 → 撰写 → 复核」这套骨架是同一套，
所以这里复刻同一套拓扑，只把 Researcher 的取证换成向量检索。

```
        START
          │
      classify            题型硬匹配（关键词，不花 token）
          │
      planner             拆成 2-4 个可独立研究的小问题
          │
      ┌───┴───┬────────┐   Send 扇出（并行）
   researcher ×N          每个单元独立做向量检索
      └───┬───┴────────┘
          │               结果用 operator.add 归约
      analyst             去重、交叉验证、找证据缺口
          │
   ┌──────┴──────┐
   │             │
 writer      no_evidence   有材料才调模型
   │             │
 reviewer         │         复核：放行 / 回 Writer 重写 / 回 Researcher 补证
   │             │
 finalize ◄──────┘         写 qa_logs（含引用快照）
   │
  END
```

四个必须做对的工程细节
---------------------
1. **``tags=["nostream"]``**：Planner/Analyst/Reviewer 用结构化输出（JSON）。
   不打这个标签，它们的 JSON 会混进 token 流，用户会看到一堆 ``{"verdict":...}``。
   只有 Writer 的正文该逐字流出。
2. **归约键**：``findings`` / ``hits`` 必须是 ``Annotated[list, operator.add]``，
   否则并行 Researcher 的结果互相覆盖，最后只剩一个单元的证据。
3. **检索可见性下推到 SQL**：见 ``vector_store``。在应用层"检索完再筛"
   会让 top-k 被筛空，表现成"库里明明有资料却答未找到依据"，而且不报错。
4. **护栏只在一处**：修订上限由 ``reviewer`` 节点入口负责，返回 END 而不是
   抛异常——用户永远拿到产出，而不是撞 LangGraph 的 recursion_limit 崩掉。

联网检索（可选，用户显式打开）
----------------------------
``retrieve`` 节点在 ``use_web`` 为真时用**原始问题**搜一次（见该节点的
docstring：聊天模式刻意只搜一次，成本可预期）。联网内容是**不可信外部数据**，
三道防线集中在 ``web_search`` 模块（``<web_content>`` 隔离块、``WEB_GUARD``
声明、出处标注）。

两个容易写错的短路点，都会让"开关点了没反应"：
- ``route_after_retrieve``：本地库为空时不能直接走"资料不足"——那恰恰是
  最需要联网的场景。
- ``route_after_analyst``：判断"有材料"必须把联网结果算进去。
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Send
from pydantic import BaseModel, Field

from app.config import (
    NO_EVIDENCE_MESSAGE,
    QUESTION_TYPE_LABELS,
    settings,
)
from app.services import storage, vector_store, web_search

logger = logging.getLogger(__name__)

# 复核回环上限。对话模式追求响应速度，默认只复核一轮。
DEFAULT_MAX_REVISIONS = 1
# 研究单元上限：扇出太多会拖长耗时且上下文膨胀
MAX_RESEARCH_UNITS = 3

# 可检索切片数不超过这个值时**跳过 Planner**，直接单单元检索。
#
# 依据（实测）：Planner 是一次独立的结构化 LLM 调用，约 1.7 秒，
# 而它唯一的产出是"把问题拆成几个子问题"。库里只有一两个切片时，
# 无论怎么拆，每个子问题检索到的都是同一批内容——
# 花 1.7 秒做一次没有信息增益的拆解，纯属浪费用户的等待时间。
PLANNER_SKIP_THRESHOLD = 2

SYSTEM_PROMPT = (
    "你是《洛克王国：世界》的 PvP 阵容与机制问答助手。"
    "你只能依据给定的参考材料回答，不能编造精灵、技能、属性克制关系或数值。"
    "如果材料不足，请明确写“资料不足”。"
    "注意：参考材料里的阵容来自玩家投稿，**不代表对局使用率或强度排名**——"
    "你可以说「有玩家这样搭配」，但不能说「这是最强阵容」。"
    "输出必须严格使用以下 Markdown 标题："
    "### 最终回答\n### 搭配思路\n### 风险提示\n"
    "如果某一部分不适用，请写“无”。"
)

# 题型判定用关键词硬匹配：这是纯分类任务，花一次模型调用不值得，
# 而且关键词匹配是确定性的——同一问题永远得到同一题型。
#
# 键与 config.QUESTION_TYPE_LABELS 对应，改这里要同步改那边。
_TYPE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    # 材料清单 -> 阵容推荐
    ("material_list", ("阵容", "搭配", "配队", "推荐什么", "带什么", "组队")),
    # 流程指引 -> 针对克制
    ("process_guide", ("克制", "针对", "怎么打", "如何应对", "反制", "被克制")),
    # 通知总结 -> 版本总结
    ("notice_summary", ("总结", "概括", "版本重点", "提炼", "改动")),
]


# --------------------------------------------------------------------------- 结构化输出模型


class ResearchUnit(BaseModel):
    """一个研究单元。Planner 拆解出来的子问题。"""

    topic: str = Field(description="要检索的具体问题，一句话，必须可独立回答")
    reason: str = Field(description="为什么这个子问题对最终结论必要")


class ResearchPlan(BaseModel):
    """Planner 的输出。"""

    brief: str = Field(description="把用户问题重述成具体、可检索的研究简报")
    units: list[ResearchUnit] = Field(description="拆解出的研究单元，2-3 个")


class Finding(BaseModel):
    """Researcher 的输出：一条带出处的发现。"""

    unit_id: str = Field(description="所属研究单元编号，如 u0")
    topic: str = Field(description="检索的问题")
    summary: str = Field(description="结论摘要")
    evidence: list[str] = Field(description="支撑证据，每条注明来自哪份文档")


class ReviewVerdict(BaseModel):
    """Reviewer 的裁决。"""

    verdict: Literal["pass", "revise"] = Field(description="pass 表示可以定稿")
    notes: str = Field(default="", description="给 Writer 的修改意见")
    evidence_gaps: list[str] = Field(
        default_factory=list,
        description="证据缺口。非空且无法靠改写弥补时，会退回 Researcher 补证",
    )
    factual_issue: bool = Field(
        default=False,
        description="回答里是否编造了资料中没有的精灵/技能/数值。只有确属编造才填 true",
    )


# --------------------------------------------------------------------------- 状态


class RagState(TypedDict, total=False):
    """图状态。

    ``total=False`` 让所有键可选——图在不同分支上填不同字段。
    ``messages`` 带 ``add_messages`` reducer：节点返回的消息会**追加**而不是覆盖。
    """

    # ---- 输入 ----
    question: str
    session_id: str
    category: str
    username: str
    is_admin: bool
    # 本轮是否允许联网检索。用户在界面上显式打开（Tavily 按次计费）。
    use_web: bool

    # ---- 预检 ----
    precheck_count: int

    # ---- Planner ----
    brief: str
    units: list[dict]

    # ---- Researcher（Send 扇出，必须归约）----
    findings: Annotated[list[dict], operator.add]
    hits: Annotated[list[dict], operator.add]
    # 本轮联网检索结果（已归一化）。
    #
    # **这里用 operator.add 而不是 sentinel reducer**：对话模式**没有**
    # checkpointer（历史走 qa_logs 重建，见 retrieve），每次请求都是一次全新的
    # 图执行，状态从 initial_state 开始，不存在"跨轮累积"的问题。
    # 攻略模式挂了 checkpointer 才需要 sentinel（见 checkpoint 模块 docstring）。
    web_results: Annotated[list[dict], operator.add]

    # ---- Analyst ----
    analysis: str
    references: list[dict]
    context: str
    evidence_gaps: list[str]

    # ---- Writer ----
    messages: Annotated[list, add_messages]
    question_type: str
    answer: str

    # ---- Reviewer ----
    verdict: str
    review_notes: str
    revision_count: int
    max_revisions: int

    # ---- 输出 ----
    qa_log_id: str
    error: str
    # 联网失败时的可读原因。**不能并进 error**——联网失败不是致命错误，
    # 回答照常产出，只是要如实告诉用户"这次没联上网"。
    web_error: str


# --------------------------------------------------------------------------- 模型


def get_chat_model(tags: list[str] | None = None, temperature: float | None = None):
    """构造对话模型。

    走 ``langchain-openai`` 的 ``ChatOpenAI``，因此任何 OpenAI 兼容端点都能用
    （DeepSeek、vLLM、One-API 等），换供应商只需改 ``.env``。

    ``tags`` 挂在**模型构造**上而不是 ``with_structured_output`` 上：
    ``with_structured_output`` 不接受 ``tags`` 关键字，传了会被当成绑定参数
    吞掉、静默失效——表现为结构化调用的 JSON 混进正文流。
    """
    if not settings.llm_api_key:
        raise RuntimeError(
            "未配置 LLM_API_KEY，无法调用对话模型。请在 backend/.env 里填写。"
        )
    from langchain_openai import ChatOpenAI

    from app.observability import LLM_HANDLER

    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=settings.llm_temperature if temperature is None else temperature,
        tags=tags or [],
        # 可观测性回调。**在构造处绑定一次**，于是所有调用路径（含
        # ``with_structured_output``）自动覆盖，不必在每个节点里传 config。
        #
        # 是模块级单例：每次 new 一个 handler 就无法把 start/end 配对，
        # 耗时与 token 都会算错。也**不要**在 invoke 时再传一遍 callbacks——
        # 那会让同一次调用被记录两次（token 翻倍）。
        callbacks=[LLM_HANDLER],
    )


def _structured(schema):
    """带 ``nostream`` 标签的结构化输出。

    标签很关键：不打的话这些调用的 JSON 会混进 ``stream_mode="messages"``，
    前端会看到 ``{"verdict": "pass"}`` 夹在正文里。

    ``method`` 走配置而不是默认值：LangChain 默认用 ``json_schema``
    （``response_format``），而 DeepSeek 会回
    ``400 This response_format type is unavailable now``。
    这个错误**不会中断流程**——它只会让节点退化到兜底分支，
    表现为"Planner 永远只拆一个单元、Researcher 的结论是原文摘要"，
    很容易被误判成模型能力问题。默认改用 ``function_calling``。
    """
    return get_chat_model(tags=["nostream"]).with_structured_output(
        schema, method=settings.structured_output_method
    )


# --------------------------------------------------------------------------- 节点：分类


def classify(state: RagState) -> dict:
    """题型硬匹配。不调模型，不花 token。"""
    lowered = (state.get("question") or "").lower()
    key = "policy_qa"
    for candidate, keywords in _TYPE_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            key = candidate
            break
    return {"question_type": QUESTION_TYPE_LABELS[key]}


# --------------------------------------------------------------------------- 节点：Planner


def planner(state: RagState) -> dict:
    """Planner：把用户问题拆成可独立检索的小问题。

    **小知识库时直接跳过**（见 ``PLANNER_SKIP_THRESHOLD``）：拆解的意义是
    让不同子问题检索到**不同**的资料；可检索切片只有一两个时，
    拆出来的每个子问题都会命中同一批内容，那次 LLM 调用（实测约 1.7 秒）
    没有任何信息增益。跳过它能让首个正文 token 早约 1.7 秒出现。
    """
    writer = get_stream_writer()
    question = state["question"]
    available = state.get("precheck_count") or 0

    if available <= PLANNER_SKIP_THRESHOLD:
        units = [{"unit_id": "u0", "topic": question, "reason": "知识库较小，直接检索原问题"}]
        writer(
            {
                "type": "agent_status",
                "agent": "Planner",
                "stage": "skipped",
                "detail": f"知识库较小（{available} 个切片），直接检索原问题",
                "percent": 20,
            }
        )
        writer(
            {
                "type": "artifact",
                "agent": "Planner",
                "kind": "plan",
                "payload": {"brief": question, "units": units, "skipped": True},
            }
        )
        return {"brief": question, "units": units}

    writer(
        {
            "type": "agent_status",
            "agent": "Planner",
            "stage": "planning",
            "detail": "正在拆解问题",
            "percent": 8,
        }
    )

    try:
        plan = _structured(ResearchPlan).invoke(
            [
                ("system", SYSTEM_PROMPT),
                (
                    "human",
                    f"用户的问题：{question}\n\n"
                    f"请把这个需求拆成 2-{MAX_RESEARCH_UNITS} 个可以独立检索的小问题。"
                    "每个小问题都要能在攻略知识库里查到资料。"
                    "如果问题本身很简单，可以只拆一个。",
                ),
            ]
        )
        # ``with_structured_output`` 在模型没产出工具调用时返回 ``None``
        # 而**不抛异常**（实测）。不挡这一下的话，``plan.units`` 会
        # AttributeError，而它在 try 里 —— 于是被下面的兜底吞掉、
        # 退化成单单元。行为上没错，但会让"模型偶发不返回工具调用"
        # 看起来像"结构化输出不支持"，日志误导排查方向。
        if plan is None:
            raise ValueError("Planner 结构化输出为空（模型未返回工具调用）")
        units = [
            {"unit_id": f"u{i}", "topic": u.topic, "reason": u.reason}
            for i, u in enumerate(plan.units[:MAX_RESEARCH_UNITS])
        ]
        brief = plan.brief
    except Exception:  # noqa: BLE001
        # 结构化输出不可用（部分兼容端点不支持 function calling）时
        # **退化成一个单元继续跑**，而不是让整个问答失败。
        # 问答是主功能，因为"拆不出子问题"就整轮报错是不可接受的。
        logger.warning("Planner 结构化输出失败，退化为单单元检索", exc_info=True)
        units = []
        brief = question

    if not units:
        units = [{"unit_id": "u0", "topic": question, "reason": "用户原始问题"}]

    writer(
        {
            "type": "artifact",
            "agent": "Planner",
            "kind": "plan",
            "payload": {"brief": brief, "units": units},
        }
    )
    return {"brief": brief, "units": units}


def dispatch_after_planner(state: RagState):
    """条件边：为每个研究单元并行派发一个 Researcher。

    **必须真的返回 ``Send`` 列表**，而不是返回一个字符串让边指向
    ``fan_out_researchers``——后者会让 planner 直接连到 researcher 节点、
    带着完整 state 跑一次（没有 topic），Send 扇出永远不会发生。
    """
    return [
        Send(
            "researcher",
            {
                "unit_id": unit["unit_id"],
                "topic": unit["topic"],
                "question": state["question"],
                "category": state.get("category") or "全部",
                "username": state.get("username") or "",
                "is_admin": bool(state.get("is_admin")),
            },
        )
        for unit in state.get("units") or []
    ]


# --------------------------------------------------------------------------- 节点：Researcher


def researcher(payload: dict) -> dict:
    """Researcher：**向量检索**知识库，产出一条带出处的 Finding。

    **这个节点会被两种方式调用，入参形状不同**，必须都处理：

    1. ``Send("researcher", {...})`` —— 收到的是**独立 payload**
       （只有 unit_id/topic/question/category/username/is_admin）。
    2. Reviewer 打回时走条件边 —— 收到的是**完整 RagState**
       （没有 unit_id/topic，但有 evidence_gaps）。

    只按第 1 种写的话，第 2 种会静默地拿空 topic 去检索，
    得到"资料不足"然后又被 Reviewer 打回，直到撞上轮次上限。
    """
    writer = get_stream_writer()

    # 分流：有 topic 说明是 Send 派发的；没有说明是复核回环进来的
    topic = (payload.get("topic") or "").strip()
    unit_id = (payload.get("unit_id") or "").strip()
    if not topic:
        gaps = payload.get("evidence_gaps") or []
        topic = gaps[0] if gaps else (payload.get("question") or "")
        unit_id = f"re{len(gaps)}" if gaps else "re0"
        if gaps:
            # 一次补一个缺口，避免并行补证导致事件交错难追踪
            payload = {**payload, "evidence_gaps": gaps[1:]}

    writer(
        {
            "type": "agent_status",
            "agent": "Researcher",
            "unit_id": unit_id,
            "stage": "retrieving",
            "detail": f"检索：{topic[:40]}",
            "percent": 25,
        }
    )

    hits = _retrieve_for_unit(payload, topic)

    if not hits:
        writer(
            {
                "type": "agent_status",
                "agent": "Researcher",
                "unit_id": unit_id,
                "stage": "empty",
                "detail": "知识库中无相关资料",
                "percent": 45,
            }
        )
        return {
            "hits": [],
            "findings": [
                {
                    "unit_id": unit_id,
                    "topic": topic,
                    "summary": "知识库中没有检索到相关资料。",
                    "evidence": [],
                }
            ],
        }

    context = _render_hits(hits)
    try:
        finding = _structured(Finding).invoke(
            [
                ("system", SYSTEM_PROMPT),
                (
                    "human",
                    f"检索问题：{topic}\n\n"
                    f"以下是知识库检索到的资料：\n\n{context}\n\n"
                    "请基于这些资料给出结论。evidence 里每条都要注明依据"
                    "（来自哪份文档）。资料不足以回答时，"
                    "summary 写「资料不足」，不要推测。",
                ),
            ]
        )
        summary, evidence = finding.summary, list(finding.evidence)
    except Exception:  # noqa: BLE001
        # 结构化失败不该丢掉已经检索到的资料——退回把原文摘要当结论，
        # 让 Writer 仍然拿得到证据。丢掉才是真正的损失。
        logger.warning("Researcher 结构化输出失败（%s），退回原文摘要", unit_id, exc_info=True)
        summary = hits[0]["content"][:300]
        evidence = [f"{hit['title']}（第 {hit['chunk_index']} 段）" for hit in hits[:3]]

    writer(
        {
            "type": "agent_status",
            "agent": "Researcher",
            "unit_id": unit_id,
            "stage": "done",
            "detail": summary[:60],
            "percent": 45,
        }
    )
    return {
        "hits": hits,
        "findings": [
            {
                "unit_id": unit_id,
                "topic": topic,
                "summary": summary,
                "evidence": evidence,
            }
        ],
    }


def _retrieve_for_unit(payload: dict, topic: str) -> list[dict]:
    """对单个研究单元做向量检索。

    可见性过滤**下推到 SQL**（``vector_store`` 内部）：普通用户只能命中
    公共库 + 自己的私有文档，root 命中全部。
    """
    try:
        return vector_store.search(
            topic,
            category=payload.get("category") or "全部",
            requester=payload.get("username") or "",
            include_all=bool(payload.get("is_admin")),
            top_k=settings.top_k,
        )
    except Exception:  # noqa: BLE001 - 单条检索失败不该拖垮整轮
        logger.exception("向量检索失败（unit=%s）", payload.get("unit_id"))
        return []


def _render_hits(hits: list[dict]) -> str:
    blocks = []
    for index, hit in enumerate(hits, start=1):
        blocks.append(
            f"[{index}] 标题：{hit['title']}\n"
            f"分类：{hit['category']} | 版本：{hit['version']} | "
            f"相似度得分：{hit['score']:.4f}\n"
            f"内容：{hit['content'][:600]}"
        )
    return "\n\n".join(blocks)


# --------------------------------------------------------------------------- 节点：Analyst


def analyst(state: RagState) -> dict:
    """Analyst：把并行检索到的切片去重、拼上下文、指出证据缺口。"""
    writer = get_stream_writer()
    findings = state.get("findings") or []
    hits = state.get("hits") or []

    writer(
        {
            "type": "agent_status",
            "agent": "Analyst",
            "stage": "cross_check",
            "detail": f"交叉验证 {len(findings)} 条发现 / {len(hits)} 个切片",
            "percent": 60,
        }
    )

    # 去重在这里做而不是在 Researcher 里：并行的多个单元很可能命中同一份文档，
    # 各自去重解决不了跨单元的重复，只有归约后才能看到全集。
    references = _build_references(hits)
    context = _build_context(hits)

    gaps = [
        finding["topic"]
        for finding in findings
        if not finding.get("evidence") or "资料不足" in (finding.get("summary") or "")
    ]

    writer(
        {
            "type": "artifact",
            "agent": "Analyst",
            "kind": "analysis",
            "payload": {
                "gaps": gaps,
                "finding_count": len(findings),
                "reference_count": len(references),
            },
        }
    )
    return {
        "analysis": _render_findings(findings),
        "references": references,
        "context": context,
        "evidence_gaps": gaps,
    }


def _build_context(hits: list[dict]) -> str:
    blocks = []
    for index, hit in enumerate(hits[: settings.max_reference_documents], start=1):
        blocks.append(
            f"[{index}] 标题：{hit['title']}\n"
            f"分类：{hit['category']} | 版本：{hit['version']} | "
            f"相似度得分：{hit['score']:.4f}\n"
            f"内容：{hit['content'][:600]}"
        )
    return "\n\n".join(blocks)


def _build_references(hits: list[dict]) -> list[dict]:
    """按 document_id 去重，保留最高分的那条。

    ``chunk_index`` 与 ``snippet`` 是前端"点开引用 → 高亮原文片段"的数据基础：
    chunk_index 用于定位，snippet 用于在卡片上直接预览。
    """
    ordered = sorted(hits, key=lambda hit: -float(hit.get("score") or 0.0))
    references: list[dict] = []
    seen: set[str] = set()
    for hit in ordered:
        document_id = hit.get("document_id")
        if not document_id or document_id in seen:
            continue
        seen.add(document_id)
        references.append(
            {
                "document_id": document_id,
                "chunk_index": hit.get("chunk_index", 0),
                "title": hit.get("title") or "未命名文档",
                "category": hit.get("category") or "未分类",
                "version": hit.get("version") or "未填写",
                "file_name": hit.get("file_name") or "",
                "score": hit.get("score", 0.0),
                "snippet": (hit.get("content") or "")[:200],
            }
        )
        if len(references) >= settings.max_reference_documents:
            break
    return references


def _render_findings(findings: list[dict]) -> str:
    if not findings:
        return "（无）"
    return "\n\n".join(
        f"### {finding.get('unit_id')} {finding.get('topic')}\n{finding.get('summary')}\n"
        + (
            "依据：" + "；".join(finding.get("evidence") or [])
            if finding.get("evidence")
            else "（无依据）"
        )
        for finding in findings
    )


# --------------------------------------------------------------------------- 节点：Writer


def generate(state: RagState) -> dict:
    """Writer：调模型生成回答。

    这是**唯一不打 ``nostream`` 的模型调用**——它的 token 要逐字推给前端。
    节点内用 ``invoke`` 而不是自己 yield：LangGraph 的
    ``stream_mode="messages"`` 会把节点内的模型 token 逐个透出。
    """
    writer = get_stream_writer()
    revision = state.get("revision_count") or 0
    writer(
        {
            "type": "agent_status",
            "agent": "Writer",
            "stage": "drafting",
            "detail": "正在撰写回答" if not revision else f"正在改写（第 {revision + 1} 轮）",
            "percent": 75,
        }
    )

    feedback = state.get("review_notes") or ""
    web_block = web_search.render_block(state.get("web_results") or [])

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", SYSTEM_PROMPT),
            # 联网内容走独立的 system 消息做隔离声明（与攻略模式同一份，
            # 见 web_search.WEB_GUARD）。**只有真的带回联网结果才加**，
            # 否则每轮都多一条无用的 system 消息。
            *([("system", web_search.WEB_GUARD)] if web_block else []),
            MessagesPlaceholder("history"),
            ("human", "{payload}"),
        ]
    )
    payload = (
        f"当前任务类型：{state.get('question_type', '')}\n"
        f"当前分类过滤：{state.get('category', '全部')}\n\n"
        f"参考材料如下：\n{state.get('context', '')}\n\n"
        f"检索结论：\n{state.get('analysis', '')}\n\n"
    )
    if web_block:
        payload += (
            f"联网检索到的外部资料（**未经验证**）：\n{web_block}\n\n"
            "引用这些内容时必须写明「来自网络、未经验证」并附来源链接；"
            "本地资料与它冲突时以本地资料为准。\n\n"
        )
    payload += f"用户问题：{state['question']}"
    if feedback and revision:
        payload += f"\n\n上一轮评审意见（请针对性修改）：\n{feedback}"

    try:
        model = get_chat_model()
        messages = prompt.format_messages(
            history=list(state.get("messages") or []),
            payload=payload,
        )
        response = model.invoke(messages)
    except Exception as exc:  # noqa: BLE001 - 模型不可用要转成可读错误
        logger.exception("模型调用失败（session_id=%s）", state.get("session_id"))
        return {"error": f"{type(exc).__name__}: {exc}"}

    content = response.content
    if isinstance(content, list):
        # 部分模型返回结构化 content（分段），拼成纯文本
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return {"answer": str(content or "")}


# --------------------------------------------------------------------------- 节点：Reviewer


def reviewer(state: RagState) -> dict:
    """Reviewer：审阅回答，决定放行还是打回。

    **打回方向分两种**，这很重要：
    - 证据不足（``evidence_gaps`` 非空）→ 回 Researcher 补检索
    - 只是写得不好 → 回 Writer 重写

    混成一个方向的话，文字问题会触发无谓的重新检索（慢且浪费），
    而证据问题会让 Writer 反复改写同一批不足的材料（永远改不好）。
    """
    writer = get_stream_writer()
    answer = state.get("answer") or ""
    revision = state.get("revision_count") or 0
    max_revisions = state.get("max_revisions") or DEFAULT_MAX_REVISIONS

    writer(
        {
            "type": "agent_status",
            "agent": "Reviewer",
            "stage": "reviewing",
            "detail": f"审阅回答（第 {revision + 1} 轮）",
            "percent": 90,
        }
    )

    # 已达上限：直接放行，不再调模型。这是**软护栏**——
    # 用户永远拿到产出，而不是撞 recursion_limit 崩掉。
    if revision >= max_revisions:
        writer(
            {
                "type": "agent_status",
                "agent": "Reviewer",
                "stage": "capped",
                "detail": f"已达最大修订轮次（{max_revisions}），放行当前版本",
                "percent": 95,
            }
        )
        return {"verdict": "pass", "review_notes": "", "revision_count": revision + 1}

    # 联网内容的隔离声明要一并给 Reviewer：否则它会把回答里
    # "来自网络"的说法当成编造（因为本地证据里确实没有）。
    review_messages: list[tuple[str, str]] = [("system", SYSTEM_PROMPT)]
    if state.get("web_results"):
        review_messages.append(("system", web_search.WEB_GUARD))
    review_messages.append(
        (
            "human",
            f"用户问题：{state['question']}\n\n"
            f"## 回答\n{answer[:4000]}\n\n"
            f"## 可用证据\n{state.get('analysis') or '（无）'}\n\n"
            + (
                f"## 联网资料（未经验证）\n"
                f"{web_search.render_block(state.get('web_results') or [])}\n\n"
                if state.get("web_results")
                else ""
            )
            + "请审阅：\n"
            "1. 有没有编造资料里没有的精灵、技能或数值？（只有确属编造才把 "
            "factual_issue 填 true）\n"
            "2. 有没有把玩家投稿说成强度排名？\n"
            "3. 关键结论是否都有依据？\n"
            "4. 引用联网资料时有没有标注「来自网络、未经验证」？"
            "把网上说法写成官方结论的算 factual_issue。\n\n"
            "**重要**：不要因为「可以更详细」「可以补充更多」就要求修改。"
            "资料不足时如实说「资料不足」是**正确**行为，不算缺陷，不要因此打回。\n"
            "只有存在下列问题时才给 verdict=revise：\n"
            "- 编造了资料中没有的内容（factual_issue=true）\n"
            "- 把玩家投稿说成了强度/使用率排名\n"
            "- 有明显的事实性错误或前后矛盾\n"
            "如果存在**证据缺口**（靠改写弥补不了），填进 evidence_gaps；"
            "只是表达问题，把修改意见填进 notes 并给 verdict=revise。"
            "两者都没有就给 verdict=pass。",
        )
    )

    try:
        verdict = _structured(ReviewVerdict).invoke(review_messages)
    except Exception:  # noqa: BLE001 - 评审失败不该让整轮崩掉
        logger.warning("Reviewer 失败，放行回答", exc_info=True)
        return {"verdict": "pass", "review_notes": "", "revision_count": revision + 1}

    # ★ 对话模式**只对事实性错误打回**，表达类问题直接放行。
    #
    # 为什么：实测一轮问答里，Reviewer 一次调用约 1.7 秒，Writer 重写约 1.9 秒。
    # 而重写发生在流式正文**已经推给用户之后**——重写的内容会再次逐字流出，
    # 前端把它**追加**到已有正文后面，用户会看到两份内容接在一起。
    # 更糟的是，若重写后第二轮评审撞上轮次上限，那次重写**根本没被校验**，
    # 等于白花 1.9 秒并牺牲了正确性。
    #
    # 对话模式追求"快且忠实"，不是"措辞完美"：把表达类打回降级为
    # 只记录意见、不改写，既省掉一次往返，也避免重复正文。
    # 攻略模式（strategy_graph）没有这个问题——它是先定稿再一次性展示报告。
    if verdict.verdict == "revise" and not verdict.factual_issue and not verdict.evidence_gaps:
        writer(
            {
                "type": "artifact",
                "agent": "Reviewer",
                "kind": "verdict",
                "payload": {
                    "verdict": "pass",
                    "notes": verdict.notes,
                    "gaps": [],
                    "downgraded": True,
                    "reason": "仅表达建议，不改写（避免重复正文与额外往返）",
                },
            }
        )
        return {
            "verdict": "pass",
            "review_notes": verdict.notes,
            "evidence_gaps": [],
            "revision_count": revision + 1,
        }

    writer(
        {
            "type": "artifact",
            "agent": "Reviewer",
            "kind": "verdict",
            "payload": {
                "verdict": verdict.verdict,
                "notes": verdict.notes,
                "gaps": verdict.evidence_gaps,
                "factual_issue": verdict.factual_issue,
            },
        }
    )
    return {
        "verdict": verdict.verdict,
        "review_notes": verdict.notes,
        "evidence_gaps": verdict.evidence_gaps,
        "revision_count": revision + 1,
    }


def route_after_review(state: RagState) -> str:
    """复核后的路由。

    **最大迭代护栏不在这里**——它在 ``reviewer`` 节点入口处。
    护栏只能有一个地方负责：放两处的话，这里的 ``revision >= max``
    会先命中并把控制权交给 finalize，导致 reviewer 里那个"超限强制放行"
    分支永远走不到。
    """
    if state.get("verdict") == "pass":
        return "finalize"
    if state.get("evidence_gaps"):
        return "researcher"  # 证据不足 -> 补检索
    return "generate"  # 只是写得不好 -> 重写


# --------------------------------------------------------------------------- 节点：无依据 / 收尾


def no_evidence(state: RagState) -> dict:
    """没检索到材料：直接给固定文案，**不调模型**。"""
    writer = get_stream_writer()
    writer(
        {
            "type": "agent_status",
            "agent": "Analyst",
            "stage": "empty",
            "detail": "知识库中没有相关资料",
            "percent": 75,
        }
    )
    return {"answer": NO_EVIDENCE_MESSAGE}


def persist(state: RagState) -> dict:
    """写 qa_logs，并补齐引用段落。

    **失败路径不落库**：模型调用报错时答案是不完整的，把它写进历史会污染
    后续轮次的上下文（下一轮会把这段残缺回答当成"之前说过的话"）。
    """
    if state.get("error"):
        return {}

    references = state.get("references") or []
    web_sources = web_search.dedupe(state.get("web_results") or [])
    body = (state.get("answer") or "").strip()
    full_answer = f"{body}\n\n### 引用文档\n{_format_reference_markdown(references)}"
    # 网络来源单独成段。**与本地引用分开写**：可信度不同，
    # 混在一起用户就分不清哪条结论有本地数据支撑。
    if web_sources:
        full_answer += f"\n\n### 网络来源（未经验证）\n{_format_web_markdown(web_sources)}"

    try:
        log = storage.add_qa_log(
            {
                "session_id": state.get("session_id") or "",
                "username": state.get("username") or "",
                "question": state["question"],
                "answer": full_answer,
                "category": state.get("category") or "全部",
                "question_type": state.get("question_type") or "",
                "source_docs": references,
                # 落库是为了刷新页面后网络来源还在（否则重新打开会话
                # 就只剩本地引用了，用户会以为联网没生效过）
                "web_sources": web_sources,
            }
        )
    except Exception as exc:  # noqa: BLE001 - 正文已经生成，只能报错收尾
        logger.exception("写问答日志失败（session_id=%s）", state.get("session_id"))
        return {"error": f"回答已生成但保存失败：{exc}"}

    return {"qa_log_id": log["id"], "answer": full_answer}


def _format_web_markdown(web_sources: list[dict]) -> str:
    """把网络来源渲染成 Markdown 列表，附原始链接。"""
    if not web_sources:
        return "无"
    return "\n".join(
        f"{index}. [{item.get('title') or '未命名'}]({item.get('url')}) —— 网络来源，未经验证"
        for index, item in enumerate(web_sources, start=1)
    )


def _format_reference_markdown(references: list[dict]) -> str:
    if not references:
        return "无"
    lines = []
    for index, item in enumerate(references, start=1):
        lines.append(
            f"{index}. {item['title']} | 分类：{item['category']} | "
            f"版本：{item['version']} | 文件：{item['file_name']}"
        )
    return "\n".join(lines)


def route_after_retrieve(state: RagState) -> str:
    """条件边：预检为空就直接走"资料不足"，不启动五 Agent 流水线。

    这是**成本护栏**：知识库为空时，五 Agent 会花 1 次 Planner + N 次
    Researcher 的模型调用，最后得出一个"没资料"的结论——既慢又费钱。

    **但用户开了联网时不能短路**：本地库为空恰恰是最需要联网的情况，
    短路掉就等于"开关点了没反应"。这时照常走流水线，
    由联网结果兜住。成本上也可接受——用户是显式要求联网的。
    """
    if state.get("precheck_count") or 0:
        return "planner"
    return "planner" if state.get("use_web") else "no_evidence"


def route_after_analyst(state: RagState) -> str:
    """条件边：有材料才走模型，否则走固定文案。

    **材料 = 本地引用 ∪ 联网结果**。只看本地引用的话，用户开着联网
    而本地库恰好没有相关内容时会被短路到"资料不足"——那正是他最需要
    联网的场景，却一次都没搜。这是"开关点了没反应"最隐蔽的一种成因。
    """
    if state.get("references") or state.get("web_results"):
        return "generate"
    return "no_evidence"


def retrieve(state: RagState) -> dict:
    """预检 + 重建对话历史 + （可选）联网检索。

    检索本身已经下放到并行的 Researcher 节点里；这里做三件事：

    1. **廉价预检**：先 COUNT 一下当前可见范围内有没有可检索的切片。
       库里一条都没有时直接短路到 ``no_evidence``，不花 Planner + N 次
       Researcher 的模型调用去"发现没资料"。纯 COUNT，不加载向量。
    2. **重建对话历史**：把历史轮次还原成消息序列。历史取的是**落库的
       完整回答**（含引用段落），与界面所见一致。
    3. **联网检索（用户显式打开时）**：用**原始问题**搜一次。

    为什么在聊天模式里是**一次**而不是每个研究单元一次
    （攻略模式是每单元一次）：对话的问题通常就是一个具体问题，
    拆出来的子问题用同一批网页基本都能覆盖；按单元搜会成倍消耗
    Tavily 额度（按次计费）而收益很小。**成本可预期**对聊天这种
    高频入口更重要。
    """
    session_id = state.get("session_id") or ""
    logs = storage.list_session_logs(session_id, limit=settings.max_history_rounds)
    messages: list[Any] = []
    for log in logs:
        messages.append(HumanMessage(content=log["question"]))
        messages.append(AIMessage(content=log["answer"]))

    available = vector_store.count_searchable(
        category=state.get("category") or "全部",
        requester=state.get("username") or "",
        include_all=bool(state.get("is_admin")),
    )

    result: dict = {
        "messages": messages,
        "units": [],
        "evidence_gaps": [],
        "precheck_count": available,
    }

    # 联网：**只在用户显式打开且服务端可用时**才真的发请求。
    # 失败一律降级（web_search.search 抛 WebSearchError），
    # 绝不能因为联网抖动让整轮问答失败——本地库才是主来源。
    if state.get("use_web") and web_search.is_configured():
        writer = get_stream_writer()
        writer(
            {
                "type": "agent_status",
                "agent": "Researcher",
                "unit_id": "web",
                "stage": "retrieving",
                "detail": "联网检索外部资料",
                "percent": 15,
            }
        )
        try:
            results = web_search.search(state.get("question") or "")
        except web_search.WebSearchError as exc:
            logger.warning("联网检索不可用：%s", exc)
            result["web_error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - 兜底：任何意外都不能让整轮崩掉
            logger.exception("联网检索意外失败")
            result["web_error"] = f"{type(exc).__name__}: {exc}"
        else:
            result["web_results"] = results
            writer(
                {
                    "type": "agent_status",
                    "agent": "Researcher",
                    "unit_id": "web",
                    "stage": "done" if results else "empty",
                    "detail": (
                        f"联网检索到 {len(results)} 条外部资料"
                        if results
                        else "联网检索没有找到相关内容"
                    ),
                    "percent": 18,
                }
            )

    return result


# --------------------------------------------------------------------------- 图


_compiled = None


def build_graph():
    """组装并编译图。编译一次后缓存——每次请求重建图是纯浪费。"""
    global _compiled
    if _compiled is not None:
        return _compiled

    builder = StateGraph(RagState)
    builder.add_node("classify", classify)
    builder.add_node("retrieve", retrieve)
    builder.add_node("planner", planner)
    builder.add_node("researcher", researcher)
    builder.add_node("analyst", analyst)
    builder.add_node("generate", generate)
    builder.add_node("no_evidence", no_evidence)
    builder.add_node("reviewer", reviewer)
    builder.add_node("persist", persist)

    builder.add_edge(START, "classify")
    builder.add_edge("classify", "retrieve")
    # 预检为空时直接短路到 no_evidence，不启动五 Agent 流水线
    builder.add_conditional_edges(
        "retrieve",
        route_after_retrieve,
        {"planner": "planner", "no_evidence": "no_evidence"},
    )
    # 注意 path_map：dispatch_after_planner 返回 Send 列表（扇出）。
    builder.add_conditional_edges("planner", dispatch_after_planner, ["researcher"])
    # 扇出后要等所有 researcher 完成才能进 analyst。
    # Send 派发的多个实例都汇聚到同一条边上，LangGraph 会等它们全部完成。
    builder.add_edge("researcher", "analyst")
    builder.add_conditional_edges(
        "analyst",
        route_after_analyst,
        {"generate": "generate", "no_evidence": "no_evidence"},
    )
    # 无依据路径不复核：固定文案没什么可审的，白花一次模型调用
    builder.add_edge("no_evidence", "persist")
    builder.add_edge("generate", "reviewer")
    builder.add_conditional_edges(
        "reviewer",
        route_after_review,
        {"finalize": "persist", "generate": "generate", "researcher": "researcher"},
    )
    builder.add_edge("persist", END)

    _compiled = builder.compile()
    return _compiled


def reset_graph() -> None:
    """测试用：丢掉已编译的图。"""
    global _compiled
    _compiled = None


def initial_state(
    question: str,
    session_id: str,
    category: str,
    username: str,
    is_admin: bool,
    max_revisions: int = DEFAULT_MAX_REVISIONS,
    use_web: bool = False,
) -> RagState:
    return RagState(
        question=question,
        session_id=session_id,
        category=category,
        username=username,
        is_admin=is_admin,
        use_web=use_web,
        precheck_count=0,
        brief="",
        units=[],
        findings=[],
        hits=[],
        web_results=[],
        analysis="",
        references=[],
        context="",
        evidence_gaps=[],
        messages=[],
        question_type="",
        answer="",
        verdict="",
        review_notes="",
        revision_count=0,
        max_revisions=max_revisions,
        qa_log_id="",
        error="",
        web_error="",
    )
