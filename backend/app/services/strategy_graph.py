"""五 Agent 深度研究流水线（LangGraph）。

Planner → Researcher(×N) → Analyst → Writer → Reviewer，带复核回环。

为什么是「图」而不是「链」
--------------------------
三个非线性的地方，链式写法都会退化成难追踪的嵌套 if：

1. **扇出**：Planner 拆出的研究单元要并行研究（``Send`` API），
   结果通过 ``operator.add`` 归约回主状态。
2. **回环**：Reviewer 可能打回给 Writer（重写）或 Researcher（补证），
   方向不同，且**必须有次数上限**。
3. **跳过**：没有可用证据时不能硬编，要走「证据不足」分支。

四个必须做对的工程细节
----------------------
1. **``tags=["nostream"]``**：Planner/Analyst/Reviewer 用结构化输出（JSON），
   如果不打这个标签，它们的 JSON 会混进 token 流，前端会看到一堆
   ``{"verdict": ...}``。只有 Writer 的正文该逐字流出。
2. **归约键**：``findings`` / ``citations`` 必须是 ``Annotated[list, operator.add]``，
   否则并行 Researcher 的结果互相覆盖，最后只剩一个。
3. **最大迭代护栏**：超限时**返回 END 而不是抛异常**——用户永远拿到产出，
   而不是撞 LangGraph 的 recursion_limit 崩掉。同时在报告里注明"未定稿"。
4. **每个进度事件带 unit_id**：并行研究时事件交错到达，
   前端靠 unit_id 分列渲染，否则进度条会互相打架。

多轮追问（Checkpointer）
-----------------------
带 ``thread_id`` 编译之后，同一会话的后续轮次能读到上一轮的研究结论与报告，
追问（「那换成水系呢」）才有主语可指。三个必须做对的点：

- ``findings`` / ``citations`` 是 ``operator.add`` 归约字段（并行扇出必需），
  但**会跨轮累积**，且输入传 ``[]`` 清不掉。所以换成 sentinel reducer
  （``checkpoint.reset_add``），每轮输入显式传 ``checkpoint.RESET``。
- ``revision_count`` 是普通字段，但 checkpoint 会把上一轮的值带进来，
  于是 Reviewer 的 ``revision + 1`` 跨轮累加，第二轮就误判"已达上限"。
  **每轮输入必须显式传 0**。
- ``turn_history`` 用有界累积，只留最近几轮，否则上下文无上限增长。
"""

from __future__ import annotations

import logging
import operator
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field

from app.config import settings
from app.domain.roco import ALL_TYPES, GAME_NAME
from app.services import checkpoint, roco_store, web_search

logger = logging.getLogger(__name__)

# 复核回环上限。超过就放行——**不抛异常**，见模块 docstring。
DEFAULT_MAX_REVISIONS = 3
# 研究单元上限：扇出太多会拖长耗时且上下文膨胀
MAX_RESEARCH_UNITS = 4


# --------------------------------------------------------------------------- 结构化输出模型


class ResearchUnit(BaseModel):
    """一个研究单元。Planner 拆解出来的子问题。"""

    topic: str = Field(description="要研究的具体问题，一句话，必须可独立回答")
    reason: str = Field(description="为什么这个子问题对最终结论必要")


class ResearchPlan(BaseModel):
    """Planner 的输出。"""

    brief: str = Field(description="把用户问题重述成具体、可执行的研究简报")
    units: list[ResearchUnit] = Field(description="拆解出的研究单元，2-4 个")


class Finding(BaseModel):
    """Researcher 的输出：一条带出处的发现。"""

    unit_id: str = Field(description="所属研究单元编号，如 u0")
    topic: str = Field(description="研究的问题")
    summary: str = Field(description="结论摘要")
    evidence: list[str] = Field(description="支撑证据，每条注明来自哪个阵容或精灵")
    lineup_ids: list[str] = Field(default_factory=list, description="引用到的阵容 id")


class Citation(BaseModel):
    n: int = Field(description="正文里的引用编号")
    kind: str = Field(description="lineup / pet / skill")
    ref_id: str = Field(description="阵容 id 或精灵名")
    title: str = Field(description="展示标题")


class ReviewVerdict(BaseModel):
    """Reviewer 的裁决。"""

    verdict: Literal["pass", "revise"] = Field(description="pass 表示可以定稿")
    notes: str = Field(default="", description="给 Writer 的修改意见")
    evidence_gaps: list[str] = Field(
        default_factory=list,
        description="证据缺口。非空且无法靠改写弥补时，会退回 Researcher 补证",
    )


# --------------------------------------------------------------------------- 状态


class StrategyState(TypedDict, total=False):
    """图状态。

    ``total=False`` 让所有键可选——图在不同分支上填不同字段，
    强制要求全部存在会让每个节点都得写一堆占位值。

    **归约字段用的是 sentinel reducer 而不是 ``operator.add``**，
    见模块 docstring「多轮追问」一节：带 checkpointer 时 ``operator.add``
    会跨轮累积，且传 ``[]`` 清不掉。
    """

    # ---- 输入 ----
    query: str
    target_pet: str
    lineup_type: str
    # 本轮是否允许联网检索。用户界面上显式打开（Tavily 按次计费）。
    use_web: bool

    # ---- Planner ----
    brief: str
    units: list[dict]

    # ---- Researcher（Send 扇出，必须归约）----
    # sentinel reducer：每轮输入传 checkpoint.RESET 才清得掉
    findings: Annotated[list[dict], checkpoint.reset_add]
    # 本轮联网检索结果（已归一化）。同样必须每轮重置，
    # 否则上一轮抓到的网页会被当成这一轮的证据。
    web_results: Annotated[list[dict], checkpoint.reset_add]

    # ---- 多轮上下文 ----
    # 最近几轮的「问题 + 结论摘要」。带 checkpointer 时跨轮保留，
    # 有界（checkpoint.MAX_TURN_HISTORY），让 Planner 能理解追问的指代。
    turn_history: Annotated[list[dict], checkpoint.bounded_add(checkpoint.MAX_TURN_HISTORY)]

    # ---- Analyst ----
    analysis: str
    evidence_gaps: list[str]

    # ---- Writer ----
    draft: str
    citations: Annotated[list[dict], checkpoint.reset_add]

    # ---- Reviewer ----
    verdict: str
    review_notes: str
    revision_count: int
    max_revisions: int

    # ---- 输出 ----
    report: str
    error: str
    degraded: bool
    # 联网失败时的可读原因。**不能并进 error**——联网失败不是致命错误，
    # 报告照常产出，只是要如实告诉用户"这次没联上网"。
    web_error: str


# --------------------------------------------------------------------------- 提示词

_SYSTEM = (
    f"你是《{GAME_NAME}》PvP 阵容攻略助手。"
    "你只能依据给定的资料作答，不得编造精灵、技能、数值或阵容。"
    "资料里的阵容来自**玩家投稿**，不代表使用率或强度排名——"
    "因此你可以说「有玩家这样搭配」，但不能说「这是最强阵容」。"
    "如果资料不足，必须明确写「资料不足」，而不是推测。"
)

# 联网内容的隔离声明。**从 web_search 取而不是在这里写**：
# 对话模式也要用同一份，各写一份早晚会改漏一处（见 web_search.WEB_GUARD）。
_WEB_GUARD = web_search.WEB_GUARD


def _model(tags: list[str] | None = None, temperature: float = 0.2):
    """构造对话模型。

    ``tags`` 会传给底层调用——``nostream`` 让这次调用的 token
    **不出现在 stream_mode="messages" 流里**。结构化输出的调用必须带上，
    否则 JSON 会污染前端的正文流。
    """
    from langchain_openai import ChatOpenAI

    from app.observability import LLM_HANDLER

    if not settings.llm_api_key:
        raise RuntimeError("未配置 LLM_API_KEY，无法调用对话模型。")
    return ChatOpenAI(
        model=settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=temperature,
        tags=tags or [],
        # 同 rag_graph.get_chat_model：构造处绑一次，覆盖全部调用路径。
        # 单例，且不要在 invoke 时重复传 callbacks（会重复计数）。
        callbacks=[LLM_HANDLER],
    )


def _structured(schema, tags: list[str] | None = None):
    """结构化输出。

    ``method`` 走配置而不是默认值：LangChain 默认用 ``json_schema``
    （``response_format``），而 DeepSeek 会回
    ``400 This response_format type is unavailable now``。
    这个错误**不会中断流程**——它只会让节点退化到兜底分支
    （Planner 只拆一个单元、Reviewer 永远放行），
    很容易被误判成模型能力问题。默认改用 ``function_calling``。
    """
    return _model(tags=tags or ["nostream"]).with_structured_output(
        schema, method=settings.structured_output_method
    )


# --------------------------------------------------------------------------- 工具：给 Researcher 的资料


def _gather_evidence(state: StrategyState, topic: str) -> dict:
    """按主题收集本地库资料（可选叠加联网结果）。

    **这是「检索」而不是「生成」**：先把真实数据取出来，再交给模型分析。
    顺序反过来的话（让模型先想，再去库里找印证）会得到看似合理但无出处的结论。

    联网结果放在 ``web`` 键里，与本地资料**分开存放**而不是混进 ``lineups``：
    渲染时要走不同的隔离块，出处标注也不同。混在一起的话，
    Writer 会分不清哪条是玩家投稿、哪条是网上抄来的。
    """
    target = (state.get("target_pet") or "").strip()
    lineup_type = state.get("lineup_type") or "pvp"

    bundle: dict[str, Any] = {
        "topic": topic,
        "lineups": [],
        "pets": [],
        "teammates": [],
        "web": [],
        "web_error": "",
    }

    # 1) 目标精灵的队友统计 —— 推荐阵容的主要依据
    if target:
        bundle["teammates"] = roco_store.teammates_of(target, lineup_type=lineup_type, limit=10)
        pet = roco_store.get_pet(target)
        if pet:
            bundle["pets"].append(
                {
                    "name": pet["name"],
                    "attributes": pet["attributes"],
                    "stats": pet["stats"],
                    "ability": pet.get("ability"),
                }
            )

    # 2) 含目标精灵的阵容（最多 6 套，够分析且不撑爆上下文）
    if target:
        bundle["lineups"] = roco_store.find_lineups_by_pet(
            target, lineup_type=lineup_type, limit=6
        )

    # 3) 主题里提到的其它精灵（例如"针对 XX"）也查一下
    for keyword_pet in _extract_pet_mentions(topic):
        if keyword_pet == target:
            continue
        pet = roco_store.get_pet(keyword_pet)
        if pet:
            bundle["pets"].append(
                {"name": pet["name"], "attributes": pet["attributes"], "stats": pet["stats"]}
            )
        extra = roco_store.find_lineups_by_pet(keyword_pet, lineup_type=lineup_type, limit=4)
        bundle["lineups"].extend(extra)

    # 3.5) 主题里提到的**属性**（"换成冰系呢"）。
    #      ★ 没有这一步，多轮追问会"记住了上下文但答不出来"：
    #      Planner 正确地把"那"补成了寂灭骨龙，但检索只按精灵名找，
    #      而这句话里唯一的实质信息是"冰系"。
    for type_name in _extract_type_mentions(topic):
        type_pets = roco_store.list_pets(attributes=[type_name], limit=8)
        for pet in type_pets:
            bundle["pets"].append(
                {"name": pet["name"], "attributes": pet["attributes"], "stats": pet["stats"]}
            )
            # 该属性精灵参与的阵容：这是"换成冰系该配谁"的答案来源
            extra = roco_store.find_lineups_by_pet(
                pet["name"], lineup_type=lineup_type, limit=2
            )
            bundle["lineups"].extend(extra)

    # 去重（按阵容 id）
    seen: set[str] = set()
    unique: list[dict] = []
    for lineup in bundle["lineups"]:
        if lineup["id"] in seen:
            continue
        seen.add(lineup["id"])
        unique.append(lineup)
    bundle["lineups"] = unique

    # 4) 联网检索（用户显式打开时才跑）。
    #    放在最后：本地资料先取好，联网失败也不影响它。
    if state.get("use_web") and web_search.is_configured():
        results, error = _gather_web(topic)
        bundle["web"] = results
        bundle["web_error"] = error

    return bundle


def _extract_pet_mentions(text: str) -> list[str]:
    """从文本里找出可能提到的精灵名。

    用「库里存在的名字出现在文本里」来判断，而不是分词——
    精灵名是专有名词，分词器未必认识，但**精确匹配一定能中**。
    """
    if not text:
        return []
    names = [p["name"] for p in roco_store.list_pets(limit=2000)]
    return [name for name in names if name and name in text]


# 属性名里有些是**日常用词**（"普通"、"光"、"水"、"地"），
# 裸匹配会把"普通玩家"、"光看数据"这类句子误判成属性查询。
# 中文没有词边界，所以要求**必须带"系"或"属性"后缀**——
# 这是指代属性的标准写法（"冰系"、"火属性"），误命中率极低。
_TYPE_SUFFIXES = ("系", "属性")


def _extract_type_mentions(text: str) -> list[str]:
    """从文本里找出提到的属性（如"换成冰系"里的"冰"）。

    **这是多轮追问能真正生效的关键**：追问常常是换一个**属性**而不是
    换一只精灵（「那换成冰系呢」）。只按精灵名检索的话，
    这句话里没有任何精灵名，Researcher 会一无所获并回答"资料不足"——
    即使库里明明有 22 只冰系精灵。表现为"上下文记住了但答不出来"。
    """
    if not text:
        return []
    found: list[str] = []
    for type_name in ALL_TYPES:
        # 「无」属性不参与克制，也不该被当成查询目标
        if type_name == "无":
            continue
        if any(f"{type_name}{suffix}" in text for suffix in _TYPE_SUFFIXES):
            found.append(type_name)
    return found


def _gather_web(topic: str) -> tuple[list[dict], str]:
    """联网检索。返回 ``(结果, 错误信息)``。

    **失败一律降级**：返回空列表 + 可读原因，由调用方转成提示。
    联网不可用时绝不能抛出去——本地库才是主来源，报告必须照常产出。
    """
    try:
        return web_search.search(topic), ""
    except web_search.WebSearchError as exc:
        return [], str(exc)
    except Exception as exc:  # noqa: BLE001 - 兜底：任何意外都不能让整轮崩掉
        logger.exception("联网检索意外失败")
        return [], f"{type(exc).__name__}: {exc}"


def _render_web(results: list[dict]) -> str:
    """把联网结果渲染进 ``<web_content>`` 隔离块。

    实现已提到 ``web_search.render_block``（对话模式共用同一份，见那里的说明）。
    """
    return web_search.render_block(results)


def _render_history(turns: list[dict]) -> str:
    """渲染最近几轮对话，给 Planner 理解追问的指代。

    只放「问题 + 结论摘要」，不放完整报告——完整报告动辄几千字，
    几轮就能把上下文挤爆，而理解指代只需要知道"上一轮聊的是什么"。
    """
    if not turns:
        return ""
    lines = ["## 之前的对话（用于理解本轮追问的指代）"]
    for turn in turns:
        lines.append(f"- 用户问：{turn.get('query', '')}")
        if turn.get("summary"):
            lines.append(f"  当时的结论：{turn['summary']}")
    return "\n".join(lines)


def _render_bundle(bundle: dict) -> str:
    """把资料渲染成给模型看的文本。"""
    lines: list[str] = []

    if bundle.get("pets"):
        lines.append("## 精灵资料")
        for pet in bundle["pets"]:
            stats = pet.get("stats") or {}
            lines.append(
                f"- {pet['name']}｜属性：{'/'.join(pet.get('attributes') or [])}｜"
                f"种族值：{stats.get('total', '?')}"
                f"（HP{stats.get('hp', '?')} 物攻{stats.get('atk', '?')} "
                f"魔攻{stats.get('sp_atk', '?')} 物防{stats.get('def', '?')} "
                f"魔防{stats.get('sp_def', '?')} 速度{stats.get('spd', '?')}）"
            )

    if bundle.get("teammates"):
        lines.append("\n## 队友搭配统计（来自玩家投稿阵容）")
        for mate in bundle["teammates"]:
            lines.append(
                f"- {mate['pet_name']}：在 {mate['together_count']}/"
                f"{mate['total_lineups']} 套同队阵容中出现"
            )

    if bundle.get("lineups"):
        lines.append("\n## 相关阵容（玩家投稿）")
        for lineup in bundle["lineups"]:
            lines.append(
                f"\n### {lineup['title']}（id={lineup['id']}，作者={lineup['author']}）"
            )
            if lineup.get("intro"):
                lines.append(f"思路：{lineup['intro']}")
            if lineup.get("blood_magic"):
                lines.append(f"血脉魔法：{lineup['blood_magic']}")
            lines.append(f"成员：{'、'.join(lineup['member_names'])}")

    if not lines:
        lines.append("（本地知识库中没有检索到相关资料）")

    # 联网内容单独成块，带 <web_content> 隔离标签。
    # **不并进上面的本地资料里**：那样模型无法区分可信度，
    # 会把网上抄来的说法和玩家投稿阵容等同看待。
    web_block = _render_web(bundle.get("web") or [])
    if web_block:
        lines.append("\n## 联网检索结果（外部内容，未经验证）")
        lines.append(web_block)
    elif bundle.get("web_error"):
        lines.append(f"\n（本次联网检索不可用：{bundle['web_error']}）")

    return "\n".join(lines)


# --------------------------------------------------------------------------- 节点


def planner(state: StrategyState) -> dict:
    """Planner：把用户问题拆成可独立研究的小问题。"""
    writer = get_stream_writer()
    writer(
        {
            "type": "agent_status",
            "agent": "Planner",
            "stage": "planning",
            "detail": "正在拆解研究问题",
            "percent": 5,
        }
    )

    target = (state.get("target_pet") or "").strip()
    hint = f"用户已指定核心精灵：{target}。" if target else ""

    # 多轮追问的关键：把最近几轮的问题与结论喂进来，Planner 才知道
    # 「那换成水系呢」里的"那"指什么。没有这段的话，拆出来的研究单元
    # 会围绕一句没有主语的句子，检索自然对不上。
    history = _render_history(state.get("turn_history") or [])

    web_hint = (
        "\n用户本轮**允许联网检索**，所以研究单元可以包含需要最新信息的问题"
        "（例如本赛季环境、新精灵）。"
        if state.get("use_web")
        else ""
    )

    messages: list[tuple[str, str]] = [("system", _SYSTEM)]
    if state.get("use_web"):
        messages.append(("system", _WEB_GUARD))
    messages.append(
        (
            "human",
            (f"{history}\n\n" if history else "")
            + f"用户的问题：{state['query']}\n{hint}{web_hint}\n\n"
            f"请把这个需求拆成 2-{MAX_RESEARCH_UNITS} 个可以独立研究的小问题。"
            "每个小问题都要能靠本地知识库（精灵资料、队友搭配统计、玩家阵容）回答。"
            "如果用户指定了精灵，第一个小问题应该是围绕它的搭配。"
            "如果上面的对话历史表明这是一句追问，请把追问补全成完整问题再拆解。",
        )
    )

    try:
        plan = _structured(ResearchPlan).invoke(messages)
    except Exception as exc:  # noqa: BLE001 - 模型不可用要转成可读错误
        logger.exception("Planner 失败")
        return {"error": f"规划失败：{type(exc).__name__}: {exc}"}

    # ``with_structured_output`` 在模型没产出工具调用时返回 ``None`` 而**不抛异常**
    # （实测）。下面 ``plan.units`` 在 try **外面**，所以不挡这一下就是
    # AttributeError -> 整个请求 500。降级成"用原始问题当唯一单元"。
    if plan is None:
        logger.warning("Planner 结构化输出为空，退化为单单元")
        plan_units = []
        brief = state["query"]
    else:
        plan_units = plan.units
        brief = plan.brief

    units = [
        {"unit_id": f"u{i}", "topic": u.topic, "reason": u.reason}
        for i, u in enumerate(plan_units[:MAX_RESEARCH_UNITS])
    ]
    if not units:
        units = [{"unit_id": "u0", "topic": state["query"], "reason": "用户原始问题"}]

    writer(
        {
            "type": "artifact",
            "agent": "Planner",
            "kind": "plan",
            "payload": {"brief": brief, "units": units},
        }
    )
    return {"brief": brief, "units": units}


def fan_out_researchers(state: StrategyState) -> list[Send]:
    """条件边：为每个研究单元并行派发一个 Researcher。

    返回 ``Send`` 列表而不是普通节点名，LangGraph 会并发执行它们，
    结果通过 ``findings`` 的 ``operator.add`` 归约回来。
    """
    return [
        Send(
            "researcher",
            {
                "unit_id": unit["unit_id"],
                "topic": unit["topic"],
                "query": state["query"],
                "target_pet": state.get("target_pet", ""),
                "lineup_type": state.get("lineup_type", "pvp"),
                # 联网开关必须显式传给每个扇出分支。
                # 漏掉的话 Send payload 里没有 use_web，_gather_evidence 读不到，
                # 表现为"用户打开了联网但一次都没联网"——不报错，静默失效。
                "use_web": bool(state.get("use_web")),
            },
        )
        for unit in state.get("units") or []
    ]


def researcher(payload: dict) -> dict:
    """Researcher：检索本地库 + 分析，产出一条带出处的 Finding。

    **这个节点会被两种方式调用，入参形状不同**，必须都处理：

    1. ``Send("researcher", {...})`` —— 收到的是**独立 payload**
       （只有 unit_id/topic/query/target_pet/lineup_type）。
    2. Reviewer 打回时走条件边 —— 收到的是**完整 StrategyState**
       （没有 unit_id/topic，但有 evidence_gaps）。

    只按第 1 种写的话，第 2 种会静默地拿空 topic 去检索，
    得到"资料不足"然后又被 Reviewer 打回，直到撞上轮次上限——
    表现为"报告里明明有缺口却一直补不上"。
    """
    writer = get_stream_writer()

    # 分流：有 topic 说明是 Send 派发的；没有说明是复核回环进来的
    topic = (payload.get("topic") or "").strip()
    unit_id = (payload.get("unit_id") or "").strip()
    if not topic:
        gaps = payload.get("evidence_gaps") or []
        topic = gaps[0] if gaps else (payload.get("query") or "")
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

    bundle = _gather_evidence(payload, topic)
    context = _render_bundle(bundle)

    # 联网结果要**归约回状态**，否则前端拿不到出处链接，
    # 用户看到"网络来源"却点不进去核对。
    web_results = bundle.get("web") or []
    web_error = bundle.get("web_error") or ""

    def _with_web(payload_out: dict) -> dict:
        """把联网结果/错误并进节点返回值。

        单独抽出来是因为这个节点有 4 个 return 分支（空库、异常、
        结构化输出为空、正常），漏掉任何一个都会导致那条路径下
        前端看不到联网出处——而且**不报错**。
        """
        if web_results:
            payload_out["web_results"] = web_results
        if web_error:
            payload_out["web_error"] = web_error
        return payload_out

    # 本地库空 + 没有联网结果 才算真的"没资料"。
    # 只看本地库的话，用户开了联网也会被这个早退挡掉——静默失效。
    if not bundle["lineups"] and not bundle["teammates"] and not bundle["pets"] and not web_results:
        writer(
            {
                "type": "agent_status",
                "agent": "Researcher",
                "unit_id": unit_id,
                "stage": "empty",
                "detail": "本地库无相关资料",
                "percent": 40,
            }
        )
        return _with_web(
            {
                "findings": [
                    {
                        "unit_id": unit_id,
                        "topic": topic,
                        "summary": "本地知识库中没有检索到相关资料。",
                        "evidence": [],
                        "lineup_ids": [],
                    }
                ]
            }
        )

    # 联网内容走独立的 system 消息做隔离声明（见 _WEB_GUARD）。
    # 只有真的带回了联网结果才加——不然每轮都多一条无用的 system 消息。
    messages: list[tuple[str, str]] = [("system", _SYSTEM)]
    if web_results:
        messages.append(("system", _WEB_GUARD))
    messages.append(
        (
            "human",
            f"研究问题：{topic}\n\n"
            f"以下是检索到的资料：\n\n{context}\n\n"
            "请基于这些资料给出结论。evidence 里每条都要注明依据"
            "（来自哪套阵容或哪个精灵；联网内容要注明来自网络、未经验证）。"
            "资料不足以回答时，summary 写「资料不足」，不要推测。",
        )
    )

    try:
        finding = _structured(Finding, tags=["nostream"]).invoke(messages)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Researcher 失败（%s）", unit_id)
        return _with_web(
            {
                "findings": [
                    {
                        "unit_id": unit_id,
                        "topic": topic,
                        "summary": f"研究失败：{type(exc).__name__}: {exc}",
                        "evidence": [],
                        "lineup_ids": [],
                    }
                ]
            }
        )

    # ``with_structured_output`` 在**模型没产出工具调用**时会返回 ``None``
    # 而不是抛异常（实测：上游偶发不返回 tool_calls 就会这样）。
    # 早先这里直接 ``finding.summary``，于是整个请求 500——
    # 一个单元的结构化输出失败不该让整轮研究崩掉，降级成一条"资料不足"即可。
    if finding is None:
        logger.warning("Researcher 结构化输出为空（unit=%s），降级为资料不足", unit_id)
        writer(
            {
                "type": "agent_status",
                "agent": "Researcher",
                "unit_id": unit_id,
                "stage": "empty",
                "detail": "模型未返回结构化结论",
                "percent": 45,
            }
        )
        return _with_web(
            {
                "findings": [
                    {
                        "unit_id": unit_id,
                        "topic": topic,
                        "summary": "模型未返回结构化结论，本单元资料不足。",
                        "evidence": [],
                        "lineup_ids": [],
                    }
                ]
            }
        )

    writer(
        {
            "type": "agent_status",
            "agent": "Researcher",
            "unit_id": unit_id,
            "stage": "done",
            "detail": finding.summary[:60],
            "percent": 45,
        }
    )
    return _with_web(
        {
            "findings": [
                {
                    "unit_id": unit_id,
                    "topic": topic,
                    "summary": finding.summary,
                    "evidence": finding.evidence,
                    "lineup_ids": finding.lineup_ids,
                }
            ]
        }
    )


def analyst(state: StrategyState) -> dict:
    """Analyst：把多条发现去重、交叉验证、指出证据缺口。"""
    writer = get_stream_writer()
    findings = state.get("findings") or []
    writer(
        {
            "type": "agent_status",
            "agent": "Analyst",
            "stage": "cross_check",
            "detail": f"正在交叉验证 {len(findings)} 条发现",
            "percent": 55,
        }
    )

    if not findings:
        return {"analysis": "没有可分析的发现。", "evidence_gaps": ["缺少任何检索结果"]}

    # 结构化汇总，便于前端展示与 Writer 引用
    rendered = "\n\n".join(
        f"### {f['unit_id']} {f['topic']}\n{f['summary']}\n"
        + ("依据：" + "；".join(f.get("evidence") or []) if f.get("evidence") else "（无依据）")
        for f in findings
    )

    # 属性层面的客观分析：这是**算出来的**，不是模型猜的
    target = (state.get("target_pet") or "").strip()
    type_note = ""
    if target:
        pets = []
        for name in _collect_member_names(findings):
            pet = roco_store.get_pet(name)
            if pet:
                pets.append({"name": pet["name"], "attributes": pet["attributes"]})
        if pets:
            analysis = roco_store.analyze_lineup_types(pets)
            # 注意：这里刻意不用嵌套同类引号的 f-string。
            # PEP 701（允许 f-string 内复用引号）是 Python 3.12+ 才有的，
            # 本项目声明 >=3.11，用了会在 3.11 上直接语法错误。
            weak = "、".join(f"{w['type']}×{w['count']}" for w in analysis["weak_to"][:6]) or "无"
            resist = "、".join(r["type"] for r in analysis["resist"][:6]) or "无"
            cover = "、".join(analysis["coverage"]) or "无"
            gaps_text = "、".join(analysis["gaps"]) or "无"
            type_note = (
                "\n\n## 属性分析（由克制矩阵计算，非模型推测）\n"
                f"- 被克制：{weak}\n"
                f"- 可抵抗：{resist}\n"
                f"- 攻击覆盖：{cover}\n"
                f"- 打不动的属性：{gaps_text}"
            )

    gaps = [
        f["topic"]
        for f in findings
        if not f.get("evidence") or "资料不足" in (f.get("summary") or "")
    ]

    writer(
        {
            "type": "artifact",
            "agent": "Analyst",
            "kind": "analysis",
            "payload": {"gaps": gaps, "finding_count": len(findings)},
        }
    )
    return {
        "analysis": rendered + type_note,
        "evidence_gaps": gaps,
    }


def _collect_member_names(findings: list[dict]) -> list[str]:
    """从发现里收集出现过的精灵名，用于属性分析。"""
    text = " ".join((f.get("summary") or "") + " ".join(f.get("evidence") or []) for f in findings)
    return _extract_pet_mentions(text)


def writer_node(state: StrategyState) -> dict:
    """Writer：写成带引用的报告。

    **这是唯一不打 ``nostream`` 的模型调用**——它的 token 要逐字推给前端。
    """
    writer = get_stream_writer()
    writer(
        {
            "type": "agent_status",
            "agent": "Writer",
            "stage": "drafting",
            "detail": "正在撰写报告",
            "percent": 70,
        }
    )

    feedback = state.get("review_notes") or ""
    revision = state.get("revision_count") or 0
    history = _render_history(state.get("turn_history") or [])

    prompt = [
        ("system", _SYSTEM),
    ]
    if state.get("web_results"):
        prompt.append(("system", _WEB_GUARD))
    prompt.append(
        (
            "human",
            (f"{history}\n\n" if history else "")
            + f"用户问题：{state['query']}\n"
            + (f"核心精灵：{state['target_pet']}\n" if state.get("target_pet") else "")
            + f"\n研究简报：{state.get('brief') or '（无）'}\n"
            f"\n## 研究发现\n{_render_findings(state.get('findings') or [])}\n"
            f"\n## 分析\n{state.get('analysis') or '（无）'}\n"
            + (f"\n## 上一轮评审意见（请针对性修改）\n{feedback}\n" if feedback and revision else "")
            + "\n请输出一份 Markdown 攻略报告，结构：\n"
            "### 结论\n### 推荐阵容\n### 属性与克制分析\n### 使用要点\n### 风险提示\n\n"
            "要求：\n"
            "1. 只依据上面的资料，不得编造精灵、技能或数值；\n"
            "2. 引用具体阵容时写出阵容标题与作者；\n"
            "3. 明确说明这些阵容来自**玩家投稿**，不代表使用率或强度排名；\n"
            "4. 资料不足的部分要直接说资料不足，不要用推测填充；\n"
            "5. 引用 <web_content> 里的内容时**必须写明来自网络、未经验证**，"
            "并附上来源链接；这类内容不得说成官方数据或确定结论。",
        )
    )

    try:
        # 不打 nostream：token 要流给前端
        response = _model(tags=["report"], temperature=0.3).invoke(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Writer 失败")
        return {"error": f"撰写失败：{type(exc).__name__}: {exc}"}

    content = response.content
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return {"draft": str(content or "")}


def _render_findings(findings: list[dict]) -> str:
    if not findings:
        return "（无）"
    return "\n\n".join(
        f"- [{f.get('unit_id')}] {f.get('topic')}：{f.get('summary')}"
        + (f"\n  依据：{'；'.join(f.get('evidence') or [])}" if f.get("evidence") else "")
        for f in findings
    )


def reviewer(state: StrategyState) -> dict:
    """Reviewer：审阅草稿，决定放行还是打回。

    **打回方向分两种**，这很重要：
    - 证据不足（``evidence_gaps`` 非空）→ 回 Researcher 补研究
    - 只是写得不好 → 回 Writer 重写

    混成一个方向的话，文字问题会触发无谓的重新检索（慢且浪费），
    而证据问题会让 Writer 反复改写同一批不足的材料（永远改不好）。
    """
    writer = get_stream_writer()
    draft = state.get("draft") or ""
    revision = state.get("revision_count") or 0
    max_revisions = state.get("max_revisions") or DEFAULT_MAX_REVISIONS

    writer(
        {
            "type": "agent_status",
            "agent": "Reviewer",
            "stage": "reviewing",
            "detail": f"审阅草稿（第 {revision + 1} 轮）",
            "percent": 85,
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

    review_messages: list[tuple[str, str]] = [("system", _SYSTEM)]
    if state.get("web_results"):
        review_messages.append(("system", _WEB_GUARD))
    review_messages.append(
        (
            "human",
            f"用户问题：{state['query']}\n\n"
            f"## 草稿\n{draft[:6000]}\n\n"
            f"## 可用证据\n{_render_findings(state.get('findings') or [])}\n\n"
            "请审阅：\n"
            "1. 有没有编造资料里没有的精灵、技能或数值？\n"
            "2. 有没有把玩家投稿说成强度排名？\n"
            "3. 关键结论是否都有依据？\n"
            "4. 引用联网内容时有没有标注「来自网络、未经验证」？"
            "把网上说法写成官方结论的一律判 revise。\n"
            "如果存在**证据缺口**（靠改写弥补不了），填进 evidence_gaps；"
            "如果只是表达问题，把修改意见填进 notes 并给 verdict=revise。\n"
            "两者都没有就给 verdict=pass。",
        )
    )

    try:
        verdict = _structured(ReviewVerdict).invoke(review_messages)
    except Exception as exc:  # noqa: BLE001 - 评审失败不该让整轮崩掉
        logger.exception("Reviewer 失败，放行草稿")
        return {"verdict": "pass", "review_notes": "", "revision_count": revision + 1}

    # 同 Planner：``with_structured_output`` 可能返回 ``None``。
    # 评审拿不到结论时**放行草稿**（fail-open）——卡住不让用户拿到答案是更糟的选择。
    if verdict is None:
        logger.warning("Reviewer 结构化输出为空，放行草稿")
        return {"verdict": "pass", "review_notes": "", "revision_count": revision + 1}

    writer(
        {
            "type": "artifact",
            "agent": "Reviewer",
            "kind": "verdict",
            "payload": {
                "verdict": verdict.verdict,
                "notes": verdict.notes,
                "gaps": verdict.evidence_gaps,
            },
        }
    )
    return {
        "verdict": verdict.verdict,
        "review_notes": verdict.notes,
        "evidence_gaps": verdict.evidence_gaps,
        "revision_count": revision + 1,
    }


def finalize(state: StrategyState) -> dict:
    """收尾：把草稿定稿，并在超限时注明未定稿。

    **同时写入本轮的多轮记忆**（``turn_history``）。这是整条链路上唯一
    知道"最终结论是什么"的地方——Analyst 只有研究摘要，Writer 的草稿
    可能被 Reviewer 打回重写。放到这里才能保证记下来的是**定稿版**。
    """
    writer = get_stream_writer()

    # 出错时**不能说"报告完成"**——那会让前端显示一个成功状态，
    # 而实际什么都没产出。用户会以为是"生成完了但内容为空"。
    if state.get("error"):
        writer(
            {
                "type": "agent_status",
                "agent": "Writer",
                "stage": "failed",
                "detail": state["error"][:80],
                "percent": 100,
            }
        )
        return {"report": ""}

    revision = state.get("revision_count") or 0
    max_revisions = state.get("max_revisions") or DEFAULT_MAX_REVISIONS
    draft = state.get("draft") or ""

    if revision > max_revisions:
        # 走到这里说明护栏触发过，如实标注而不是假装完美
        draft += (
            f"\n\n---\n\n> 注：本报告在达到最大修订轮次（{max_revisions}）后放行，"
            "可能仍有未解决的问题。"
        )

    writer(
        {
            "type": "agent_status",
            "agent": "Writer",
            "stage": "final",
            "detail": "报告完成",
            "percent": 100,
        }
    )

    # 记忆里只放摘要（前 200 字），不放完整报告：完整报告几千字，
    # 几轮就撑爆上下文，而理解追问的指代只需要知道结论大意。
    # reducer 是 bounded_add(MAX_TURN_HISTORY)，会自动裁掉更早的轮次。
    summary = draft.strip().replace("\n", " ")[:200]
    return {
        "report": draft,
        "turn_history": [
            {
                "query": state.get("query") or "",
                "target_pet": state.get("target_pet") or "",
                "summary": summary,
            }
        ],
    }


def route_after_review(state: StrategyState) -> str:
    """复核后的路由。

    **最大迭代护栏不在这里**——它在 ``reviewer`` 节点入口处。
    护栏只能有一个地方负责：放两处的话，这里的 ``revision >= max``
    会先命中并把控制权交给 finalize，导致 reviewer 里那个"超限强制放行"
    分支永远走不到，``capped`` 标记也就永远设不上——
    表现为「报告明明没定稿，却没有任何提示」。
    """
    if state.get("verdict") == "pass":
        return "finalize"
    if state.get("evidence_gaps"):
        return "researcher"  # 证据不足 -> 补研究
    return "writer"  # 只是写得不好 -> 重写


def dispatch_after_planner(state: StrategyState):
    """Planner 之后的分派。

    **这个函数必须真的返回 ``Send`` 列表**，而不是返回一个字符串让边指向
    ``fan_out_researchers``——后者会让 planner 直接连到 researcher 节点、
    带着完整 state 跑一次（没有 topic），Send 扇出永远不会发生。
    症状很隐蔽：图能跑完，但只有一次研究、且 topic 为空。
    """
    if state.get("error"):
        return "finalize"
    return fan_out_researchers(state)


# --------------------------------------------------------------------------- 图


_compiled = None


def build_graph(*, with_checkpointer: bool = True):
    """组装并编译。

    ``with_checkpointer=True``（默认）时挂上 checkpointer，**这样每个调用
    都必须传 ``thread_id``**，否则 LangGraph 直接抛
    ``ValueError: Checkpointer requires one or more of the following
    'configurable' keys: thread_id, ...``——不是静默退化成无状态。

    ``with_checkpointer=False`` 给测试用（不关心多轮时省掉建表和 thread_id）。
    """
    global _compiled
    if with_checkpointer and _compiled is not None:
        return _compiled

    builder = StateGraph(StrategyState)
    builder.add_node("planner", planner)
    builder.add_node("researcher", researcher)
    builder.add_node("analyst", analyst)
    builder.add_node("writer", writer_node)
    builder.add_node("reviewer", reviewer)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "planner")
    # 注意 path_map：dispatch_after_planner 要么返回 Send 列表（扇出），
    # 要么返回字符串 "finalize"。两种情况都要在映射里，否则出错分支会报
    # "unknown node"。
    builder.add_conditional_edges(
        "planner",
        dispatch_after_planner,
        ["researcher", "finalize"],
    )
    # 扇出后要等所有 researcher 完成才能进 analyst。
    # Send 派发的多个实例都汇聚到同一条边上，LangGraph 会等它们全部完成。
    builder.add_edge("researcher", "analyst")
    builder.add_edge("analyst", "writer")
    builder.add_edge("writer", "reviewer")
    builder.add_conditional_edges(
        "reviewer",
        route_after_review,
        {"finalize": "finalize", "writer": "writer", "researcher": "researcher"},
    )
    builder.add_edge("finalize", END)

    if not with_checkpointer:
        return builder.compile()

    saver = checkpoint.get_saver()
    if saver is None:
        # checkpoint_enabled=false：退化成无状态。这里**不缓存**，
        # 否则改配置后必须重启才生效（测试里会踩到）。
        logger.info("checkpoint_enabled=false，攻略图以无状态方式编译")
        return builder.compile()

    compiled = builder.compile(checkpointer=saver)
    _compiled = compiled
    return compiled


def reset_graph() -> None:
    """测试用：丢掉已编译的图。"""
    global _compiled
    _compiled = None


def initial_state(
    query: str,
    target_pet: str = "",
    lineup_type: str = "pvp",
    max_revisions: int = DEFAULT_MAX_REVISIONS,
    use_web: bool = False,
) -> StrategyState:
    """构造本轮输入状态。

    **归约字段一律传 ``checkpoint.RESET`` 而不是 ``[]``**（见模块 docstring）：
    带 checkpointer 时 ``[]`` 清不掉上一轮的累积值。
    ``revision_count`` 必须显式传 0——否则 Reviewer 的 ``revision + 1``
    会跨轮累加，第二轮就误判"已达上限"直接放行。
    ``turn_history`` **不传**：它是要跨轮保留的，传了反而清掉。
    """
    return StrategyState(
        query=query,
        target_pet=target_pet,
        lineup_type=lineup_type,
        use_web=use_web,
        brief="",
        units=[],
        findings=checkpoint.RESET,
        web_results=checkpoint.RESET,
        analysis="",
        evidence_gaps=[],
        draft="",
        citations=checkpoint.RESET,
        verdict="",
        review_notes="",
        revision_count=0,
        max_revisions=max_revisions,
        report="",
        error="",
        web_error="",
        degraded=False,
    )
