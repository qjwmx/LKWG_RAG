"""无需登录的公开接口。

只有一个用途：给**登录页**提供展示用的精灵数据（头像 + 属性），
让未登录用户一进来就能看到这是洛克王国的项目。

为什么单独一个 router：``roco.router`` 在 ``main.py`` 里是挂
``get_current_user`` 依赖的（整个领域接口都要登录）。而登录页必须
在**未登录**状态下就能取到数据，所以这里的路由不能带那个依赖。

**安全边界**：这里只暴露精灵图鉴的公开信息（名字、属性、头像、种族值），
不涉及任何用户数据、文档、会话或问答记录。加新接口前请先确认这一点——
公开路由是最容易被无意中扩大权限的地方。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.schemas import BaseResponse
from app.services import roco_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["公开接口"])

# 展示用的精灵数量。太多会挤满登录页，太少看不出效果。
SHOWCASE_LIMIT = 12


@router.get("/showcase", summary="展示用精灵（无需登录）")
def showcase() -> BaseResponse:
    """挑一批有头像的精灵给登录页做动画。

    **挑选规则是确定性的**（同一部署每次刷新都一样），但**按属性轮转**：
    单纯按种族值取前 N 只会得到清一色的机械/地系，动画看起来像一坨同色块。
    按属性轮流取，画面上才有颜色层次。

    没有头像的会被过滤掉——否则动画里会混进一堆首字占位块。
    """
    # 取足够大的候选池，才有多样性可挑
    candidates = [p for p in roco_store.list_pets(sort="total_desc", limit=200) if p.get("image_url")]

    picked: list[dict] = []
    used_attributes: set[str] = set()
    used_names: set[str] = set()

    # 第一轮：每个属性最多先取 1 只，保证颜色分散
    for pet in candidates:
        primary = (pet.get("attributes") or ["未知"])[0]
        if primary in used_attributes:
            continue
        picked.append(pet)
        used_attributes.add(primary)
        used_names.add(pet["name"])
        if len(picked) >= SHOWCASE_LIMIT:
            break

    # 第二轮：属性种类不够 12 种时，用剩下的高种族值精灵补满
    if len(picked) < SHOWCASE_LIMIT:
        for pet in candidates:
            if pet["name"] in used_names:
                continue
            picked.append(pet)
            used_names.add(pet["name"])
            if len(picked) >= SHOWCASE_LIMIT:
                break

    # 顺带给登录页一些能展示"这个库有多大"的数字
    stats = roco_store.stats()

    return BaseResponse.success(
        data={
            "pets": [
                {
                    "name": p["name"],
                    "no": p["no"],
                    "attributes": p["attributes"],
                    "image_url": p["image_url"],
                    "total": (p.get("stats") or {}).get("total", 0),
                }
                for p in picked
            ],
            "stats": {
                "pets": stats.get("pets", 0),
                "lineups": stats.get("lineups_total", 0),
            },
        }
    )
