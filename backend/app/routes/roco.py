"""洛克王国领域接口：图鉴、阵容、攻略研究。

三组接口：

- ``/pokedex/*`` —— 精灵与技能图鉴（选精灵的 UI 用）
- ``/lineups/*`` —— 阵容查询（**含按精灵反查**，这是核心能力）
- ``/strategy/*`` —— 五 Agent 攻略研究（SSE 流式）

统一响应约定与既有接口一致：``{code, msg, data}``，HTTP 恒为 200；
401/403 仍走真实状态码。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.domain.roco import GAME_NAME, LINEUP_FILTER_OPTIONS
from app.schemas import CODE_BAD_REQUEST, CODE_NOT_FOUND, BaseResponse
from app.security import CurrentUser, get_current_user
from app.services import roco_store, strategy_service, web_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["洛克王国"])

# SSE 响应头。``X-Accel-Buffering: no`` 不能省——
# 挡在中间的 Nginx 默认会缓冲整个响应，流式就变成"一次性吐出"。
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


# --------------------------------------------------------------------------- 图鉴


@router.get("/pokedex/pets", summary="精灵图鉴列表（支持属性筛选与排序）")
def list_pets(
    keyword: str = "",
    attributes: str = "",
    min_total: int = 0,
    sort: str = "no",
    limit: int = 60,
    offset: int = 0,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """精灵列表。

    ``attributes`` 是**逗号分隔**的属性名（如 ``火,水``），语义是
    **任一命中**（OR）——勾选两个属性是想看这两系都有谁，
    而不是找同时是火水的双属性精灵。

    ``sort`` 可取 ``no`` / ``total_desc`` / ``total_asc`` / ``speed_desc`` / ``name``。
    """
    attr_list = [a.strip() for a in attributes.split(",") if a.strip()]
    limit = max(1, min(limit, 200))
    return BaseResponse.success(
        data={
            "pets": roco_store.list_pets(
                keyword=keyword,
                attributes=attr_list,
                min_total=min_total,
                sort=sort,
                limit=limit,
                offset=offset,
            ),
            "total": roco_store.count_pets(
                keyword=keyword, attributes=attr_list, min_total=min_total
            ),
            "attribute_counts": roco_store.attribute_counts(),
        }
    )


@router.get("/pokedex/pet", summary="精灵详情")
def get_pet(
    name: str,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    pet = roco_store.get_pet(name)
    if not pet:
        return BaseResponse.error(f"未找到精灵：{name}", code=CODE_NOT_FOUND)
    # 技能详情一并给出，前端不必再请求一次
    pet["skills"] = roco_store.get_skills(pet.get("skill_names") or [])
    return BaseResponse.success(data=pet)


@router.get("/pokedex/skills", summary="技能图鉴")
def list_skills(
    keyword: str = "",
    limit: int = 100,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    limit = max(1, min(limit, 300))
    return BaseResponse.success(data=roco_store.list_skills(keyword=keyword, limit=limit))


@router.get("/pokedex/types", summary="属性克制表")
def type_chart(user: CurrentUser = Depends(get_current_user)) -> BaseResponse:
    """整张克制矩阵。前端画克制表用。"""
    from app.domain.roco import ALL_TYPES, TYPE_MATCHUP

    return BaseResponse.success(
        data={
            "types": ALL_TYPES,
            "matchup": TYPE_MATCHUP,
        }
    )


@router.get("/pokedex/stats", summary="领域数据概览")
def domain_stats(user: CurrentUser = Depends(get_current_user)) -> BaseResponse:
    """数据规模 + 时效说明。

    **必须把"数据来自玩家投稿"讲清楚**：它不代表使用率或强度，
    前端要把它显示给用户，不能让推荐结果看起来像权威排名。
    """
    from app.scraper.seed_loader import seed_status

    return BaseResponse.success(
        data={
            "game": GAME_NAME,
            **seed_status(),
            "notice": (
                "阵容数据来自 BWIKI 的玩家投稿，不代表对局使用率或强度排名。"
                "数据会随赛季更新而过时，请以游戏内实际情况为准。"
            ),
            "lineup_filter_options": LINEUP_FILTER_OPTIONS,
        }
    )


# --------------------------------------------------------------------------- 阵容


@router.get("/lineups/by_pet", summary="★ 按精灵反查阵容")
def lineups_by_pet(
    pet_name: str,
    lineup_type: str = "pvp",
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """**核心能力**：找出包含指定精灵的阵容。

    走 ``lineup_members.pet_base_name`` 索引精确命中，
    不是向量相似度——所以"有 32 套阵容含这只精灵"这个数字是准的。

    同时返回**队友搭配统计**：真实投稿里和它同队最多的精灵。
    这是推荐阵容的数据依据（而不是让模型凭空想搭配）。
    """
    if not pet_name.strip():
        return BaseResponse.error("请指定精灵名。", code=CODE_BAD_REQUEST)

    lineups = roco_store.find_lineups_by_pet(pet_name, lineup_type=lineup_type)
    return BaseResponse.success(
        data={
            "pet_name": pet_name,
            "total": len(lineups),
            "lineups": lineups,
            "teammates": roco_store.teammates_of(pet_name, lineup_type=lineup_type, limit=12),
        }
    )


@router.get("/lineups/list", summary="阵容列表")
def list_lineups(
    lineup_type: str = "pvp",
    keyword: str = "",
    limit: int = 30,
    offset: int = 0,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    limit = max(1, min(limit, 100))
    return BaseResponse.success(
        data={
            "lineups": roco_store.list_lineups(
                lineup_type=lineup_type, keyword=keyword, limit=limit, offset=offset
            ),
            "total": roco_store.count_lineups(lineup_type),
        }
    )


@router.get("/lineups/detail", summary="阵容详情（含成员与技能）")
def lineup_detail(
    lineup_id: str,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    lineup = roco_store.get_lineup(lineup_id)
    if not lineup:
        return BaseResponse.error("未找到该阵容。", code=CODE_NOT_FOUND)

    # 顺带算出属性攻防，前端可以直接画风险条
    pets = []
    for name in lineup.get("member_names") or []:
        pet = roco_store.get_pet(name)
        if pet:
            pets.append({"name": pet["name"], "attributes": pet["attributes"]})
    lineup["type_analysis"] = roco_store.analyze_lineup_types(pets) if pets else None
    return BaseResponse.success(data=lineup)


@router.get("/lineups/top_pets", summary="出场最多的精灵")
def top_pets(
    limit: int = 20,
    lineup_type: str = "pvp",
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """投稿里出现次数最多的精灵。

    ⚠️ 这是**投稿出现次数**，不是使用率或胜率。响应里带 notice 字段提醒，
    前端必须展示——把它包装成"版本强势榜"是误导。
    """
    limit = max(1, min(limit, 60))
    return BaseResponse.success(
        data={
            "pets": roco_store.top_pets_in_lineups(limit=limit, lineup_type=lineup_type),
            "notice": "统计口径为玩家投稿阵容中的出现次数，不代表对局使用率或强度排名。",
        }
    )


# --------------------------------------------------------------------------- 攻略研究


class StrategyRequest(BaseModel):
    query: str = Field(..., min_length=1, description="用户的问题")
    target_pet: str = Field("", description="核心精灵，可空")
    lineup_type: str = Field("pvp", description="pvp / pve / 全部")
    max_revisions: int = Field(3, ge=0, le=6, description="最大修订轮次")
    # 多轮追问的会话标识。前端生成并复用；**服务端会再拼上用户名前缀**
    # （见 checkpoint.thread_id_for），所以传别人的 session_id 读不到别人的上下文。
    session_id: str = Field("", max_length=96, description="攻略会话 id，用于多轮追问")
    # 联网检索开关。**默认关**：Tavily 按次计费，且联网内容是外部不可信数据。
    use_web: bool = Field(False, description="本轮是否允许联网检索")


@router.get("/strategy/web_status", summary="联网检索可用性")
def strategy_web_status(user: CurrentUser = Depends(get_current_user)) -> BaseResponse:
    """前端用它决定「允许联网」按钮是否可点。

    **不返回任何 key 内容**，只说有没有配——前端只需要知道能不能用。
    放在鉴权后面：不必让未登录的人探测服务端配置。
    """
    return BaseResponse.success(data=web_search.status())


@router.post("/strategy/research", summary="★ 五 Agent 攻略研究（SSE）")
def strategy_research(
    payload: StrategyRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """跑 Planner→Researcher→Analyst→Writer→Reviewer 流水线，SSE 流式返回。

    事件类型见 ``strategy_service`` 的模块说明。要点：
    - ``agent_status`` 驱动前端的五张 Agent 卡片
    - ``delta`` 只有 Writer 的正文（结构化输出已用 nostream 标签挡掉）
    - ``done`` 带完整报告、引用阵容、修订轮数与是否超限放行
    - ``web_unavailable`` —— 用户开了联网但服务端没配，提前告知
    """
    return StreamingResponse(
        # **必须编码成 SSE 帧字符串**：StreamingResponse 直接吐 dict 会抛
        # AttributeError: 'dict' object has no attribute 'encode'。
        # 服务层刻意产出 dict（便于非流式复用与测试），编码放在路由层。
        (
            strategy_service.sse_frame(event)
            for event in strategy_service.strategy_stream(
                query=payload.query,
                target_pet=payload.target_pet,
                lineup_type=payload.lineup_type,
                max_revisions=payload.max_revisions,
                use_web=payload.use_web,
                username=user.username,
                session_id=payload.session_id,
            )
        ),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/strategy/research_sync", summary="五 Agent 攻略研究（非流式）")
def strategy_research_sync(
    payload: StrategyRequest,
    user: CurrentUser = Depends(get_current_user),
) -> BaseResponse:
    """非流式版本。调试与脚本用；前端主链路走 SSE。"""
    try:
        final = strategy_service.run_strategy(
            query=payload.query,
            target_pet=payload.target_pet,
            lineup_type=payload.lineup_type,
            max_revisions=payload.max_revisions,
            use_web=payload.use_web,
            username=user.username,
            session_id=payload.session_id,
        )
    except Exception as exc:  # noqa: BLE001 - 转成可读错误而不是 500
        logger.exception("攻略研究失败（query=%s）", payload.query)
        return BaseResponse.error(f"研究失败：{type(exc).__name__}: {exc}")

    lineup_ids: list[str] = []
    for finding in final.get("findings") or []:
        lineup_ids.extend(finding.get("lineup_ids") or [])

    return BaseResponse.success(
        data={
            "report": final.get("report") or "",
            "lineups": strategy_service._render_lineups(lineup_ids),
            "units": final.get("units") or [],
            "findings": final.get("findings") or [],
            "revisions": final.get("revision_count") or 0,
            "gaps": final.get("evidence_gaps") or [],
            # 与 SSE 版本保持一致：联网出处与失败原因都要带回去
            "web_sources": strategy_service._dedupe_web(final.get("web_results") or []),
            "web_error": final.get("web_error") or "",
            "session_id": payload.session_id,
        }
    )
