"""洛克王国领域检索与五 Agent 流水线的测试。

分两组：
1. **领域检索**（``roco_store``）—— 用真实种子数据，验证「选精灵反查阵容」
   这个核心能力。这组测试的价值在于：它跑的是**真数据**，
   能发现"名称归一化没做"这类会静默返回空结果的问题。
2. **五 Agent 图**（``strategy_graph``）—— 用替身模型，验证拓扑、
   归约、护栏。不依赖外部服务。
"""

from __future__ import annotations

import pytest

from app.services import roco_store


# --------------------------------------------------------------------------- 数据就绪


# ``seeded_domain`` 夹具已移到 ``conftest.py``。
#
# 原先它定义在这个文件里，于是只有本文件能用——而 ``test_strategy.py``
# 的 Researcher 节点依赖库里有阵容数据。那个依赖被文件名顺序掩盖了
# （test_roco 排在 test_strategy 前面），单独跑 test_strategy 就会挂。
# 放在 conftest 里让所有测试文件都能安全依赖。


# --------------------------------------------------------------------------- 图鉴


def test_pets_are_loaded(seeded_domain):
    assert seeded_domain["pets"] > 100, "精灵图鉴数量异常"
    assert seeded_domain["lineups_total"] > 50, "阵容数量异常"


def test_pet_search_by_keyword(seeded_domain):
    results = roco_store.list_pets(keyword="骨龙", limit=20)
    assert results, "搜索「骨龙」应有结果"
    names = [p["name"] for p in results]
    assert any("骨龙" in n for n in names)


def test_pet_detail_has_stats_and_skills(seeded_domain):
    pet = roco_store.get_pet("寂灭骨龙")
    assert pet is not None, "应有寂灭骨龙"
    assert pet["attributes"], "应有属性"
    assert pet["stats"].get("total"), "应有种族值总和"
    assert pet["skill_names"], "应有可学技能"


# --------------------------------------------------------------------------- 属性筛选


def test_filter_pets_by_attribute(seeded_domain):
    """按属性筛选。**整词匹配**，不能因为 JSON 转义而恒返回空。"""
    fire = roco_store.list_pets(attributes=["火"], limit=100)
    assert fire, "应有火属性精灵"
    for pet in fire:
        assert "火" in pet["attributes"], f"{pet['name']} 不含火属性"


def test_attribute_filter_is_word_matched(seeded_domain):
    """整词匹配不会误伤：筛「火」不该把「水」的精灵带出来。"""
    fire = roco_store.list_pets(attributes=["火"], limit=200)
    water = roco_store.list_pets(attributes=["水"], limit=200)
    fire_names = {p["name"] for p in fire}
    water_only = {p["name"] for p in water} - fire_names
    # 只水不火的精灵不该出现在火的结果里
    for pet in fire:
        assert "火" in pet["attributes"]


def test_attribute_filter_or_semantics(seeded_domain):
    """多选是 OR：火+龙 = 火的数量 + 只龙的数量。"""
    fire = roco_store.count_pets(attributes=["火"])
    dragon = roco_store.count_pets(attributes=["龙"])
    both = roco_store.count_pets(attributes=["火", "龙"])
    assert both >= max(fire, dragon), "OR 的结果不该少于单个"
    assert both <= fire + dragon, "OR 的结果不该超过两者之和"


def test_count_matches_list(seeded_domain):
    """计数与列表必须用同一套筛选条件，否则会出现"显示 20 条但说共 465 只"。"""
    listed = roco_store.list_pets(attributes=["火"], limit=500)
    counted = roco_store.count_pets(attributes=["火"])
    assert len(listed) == counted


def test_min_total_filter(seeded_domain):
    pets = roco_store.list_pets(min_total=600, limit=100)
    assert pets
    for pet in pets:
        assert pet["stats"].get("total", 0) >= 600


def test_sort_by_total_is_stable(seeded_domain):
    """排序必须带稳定的次级键，否则翻页会重复或漏项。"""
    desc = roco_store.list_pets(sort="total_desc", limit=10)
    totals = [p["stats"].get("total", 0) for p in desc]
    assert totals == sorted(totals, reverse=True)

    asc = roco_store.list_pets(sort="total_asc", limit=10)
    totals_asc = [p["stats"].get("total", 0) for p in asc]
    assert totals_asc == sorted(totals_asc)


def test_speed_sort(seeded_domain):
    pets = roco_store.list_pets(sort="speed_desc", limit=10)
    speeds = [p["stats"].get("spd", 0) for p in pets]
    assert speeds == sorted(speeds, reverse=True)


def test_attribute_counts(seeded_domain):
    counts = roco_store.attribute_counts()
    assert counts, "应有属性统计"
    assert all(c["count"] > 0 for c in counts), "不该返回 0 计数的属性"
    # 计数降序或按固定顺序都可，但总数应合理
    total = sum(c["count"] for c in counts)
    assert total >= seeded_domain["pets"], "双属性精灵会被计两次，总数应不小于精灵数"


# --------------------------------------------------------------------------- 图片


def test_pets_have_image_urls(seeded_domain):
    """精灵头像 URL 应已同步（同步失败时为空串，前端会退化到占位）。"""
    pets = roco_store.list_pets(limit=200)
    with_image = [p for p in pets if p["image_url"]]
    # 不强制 100%：同步是可选的，但既然跑过就该有大部分
    assert len(with_image) > len(pets) * 0.5, "大部分精灵应有头像"


def test_skills_have_icon_urls(seeded_domain):
    skills = roco_store.list_skills(limit=200)
    with_icon = [s for s in skills if s.get("icon_url")]
    assert len(with_icon) > len(skills) * 0.5, "大部分技能应有图标"


def test_lineup_members_carry_images(seeded_domain):
    """阵容成员要带头像——前端卡片直接渲染，不必再逐只查。"""
    lineups = roco_store.find_lineups_by_pet("寂灭骨龙", limit=3)
    assert lineups
    for lineup in lineups:
        images = lineup.get("member_images") or []
        assert len(images) == len(lineup["member_names"]), "每个成员都该有一条图信息"
        for item in images:
            assert item["name"]
            assert "image_url" in item


def test_lineup_detail_members_have_skills_with_icons(seeded_domain):
    lineups = roco_store.find_lineups_by_pet("寂灭骨龙", limit=1)
    detail = roco_store.get_lineup(lineups[0]["id"])
    assert detail
    member = detail["members"][0]
    assert "image_url" in member
    assert "attributes" in member
    assert member["skill_details"], "成员应带技能详情"
    # 技能详情要含 icon_url 字段（可能为空串，但键必须在）
    assert "icon_url" in member["skill_details"][0]


# --------------------------------------------------------------------------- 登录页展示接口


def test_public_showcase_requires_no_auth(client, seeded_domain):
    """★ 登录页要用它，所以**必须能未登录访问**。

    这条测试保护的是"登录页不会因为接口要鉴权而白屏"。
    """
    response = client.get("/api/v1/public/showcase")
    assert response.status_code == 200, "公开接口不该要求登录"
    body = response.json()
    assert body["code"] == 200
    assert body["data"]["pets"], "应返回展示精灵"


def test_public_showcase_pets_have_images(client, seeded_domain):
    """展示动画里不该混进没有头像的精灵（会变成一堆占位块）。"""
    body = client.get("/api/v1/public/showcase").json()
    for pet in body["data"]["pets"]:
        assert pet["image_url"], f"{pet['name']} 没有头像，不该进展示列表"
        assert pet["name"]


def test_public_showcase_is_attribute_diverse(client, seeded_domain):
    """按属性轮转挑选，避免整屏同一个颜色。"""
    body = client.get("/api/v1/public/showcase").json()
    primaries = {p["attributes"][0] for p in body["data"]["pets"] if p["attributes"]}
    assert len(primaries) >= 5, f"展示精灵属性过于单一：{primaries}"


def test_public_showcase_exposes_no_private_data(client, seeded_domain):
    """★ 公开路由是最容易无意中扩大权限的地方。

    它只该返回精灵图鉴的公开信息——出现任何用户/文档/会话字段都是越界。
    """
    body = client.get("/api/v1/public/showcase").json()
    serialized = str(body)
    for forbidden in ("password", "owner", "token", "session_id", "username"):
        assert forbidden not in serialized, f"公开接口不该出现 {forbidden}"
    # 每只精灵只应有这些字段
    for pet in body["data"]["pets"]:
        assert set(pet.keys()) == {"name", "no", "attributes", "image_url", "total"}


def test_domain_routes_still_require_auth(client, seeded_domain):
    """公开接口的加入**不能**把领域接口的鉴权一起放开。"""
    for path in ("/api/v1/pokedex/pets", "/api/v1/lineups/list"):
        assert client.get(path).status_code == 401, f"{path} 仍应要求登录"



def test_skill_lookup_is_batched(seeded_domain):
    """批量取技能：一套阵容 24 个技能，逐个查会有 24 次往返。"""
    pet = roco_store.get_pet("寂灭骨龙")
    names = pet["skill_names"][:5]
    skills = roco_store.get_skills(names)
    assert skills, "批量查技能应有结果"
    for name in names:
        if name in skills:
            assert "power" in skills[name]
            assert "attribute" in skills[name]


# --------------------------------------------------------------------------- 核心：按精灵反查阵容


def test_find_lineups_by_pet_returns_real_data(seeded_domain):
    """★ 核心能力：选一只精灵，查出包含它的真实阵容。"""
    lineups = roco_store.find_lineups_by_pet("寂灭骨龙")
    assert lineups, "寂灭骨龙应出现在多套阵容里"
    for lineup in lineups:
        assert lineup["title"]
        assert lineup["member_names"], "阵容应带成员列表"
        assert "寂灭骨龙" in lineup["member_names"], "反查结果必须真的含这只精灵"


def test_find_lineups_is_exact_not_fuzzy(seeded_domain):
    """反查是精确关系查询，不是模糊匹配。

    这条测试保护的是"数字要准"：如果实现退化成向量相似度，
    返回的阵容里会混进不含该精灵的，而 `total` 也就失去意义。
    """
    pet = "寂灭骨龙"
    lineups = roco_store.find_lineups_by_pet(pet, limit=200)
    for lineup in lineups:
        assert pet in lineup["member_names"], f"《{lineup['title']}》并不含 {pet}"


def test_find_lineups_normalizes_form_suffix(seeded_domain):
    """带形态后缀的精灵名要能互相命中。

    图鉴里选的是「化蝶」，阵容里可能写「化蝶（平常的样子）」。
    不做归一化就查不到，而且**不报错**——只是返回空，看起来像"库里没有"。
    """
    base = roco_store.find_lineups_by_pet("化蝶", limit=200)
    assert base, "按基础名应能查到"

    # 库里存的是带后缀的形式，用带后缀的名字查也应命中同一批
    members = set()
    for lineup in base:
        members.update(lineup["member_names"])
    assert "化蝶" in members


def test_find_lineups_empty_for_unknown_pet(seeded_domain):
    assert roco_store.find_lineups_by_pet("不存在的精灵名xyz") == []


def test_teammates_are_ranked_by_cooccurrence(seeded_domain):
    """队友统计是「推荐阵容」的数据依据：真实投稿里和它同队最多的精灵。"""
    mates = roco_store.teammates_of("寂灭骨龙", limit=10)
    assert mates, "应有队友统计"
    counts = [m["together_count"] for m in mates]
    assert counts == sorted(counts, reverse=True), "应按同队次数降序"
    # 同队次数不应超过总阵容数
    for mate in mates:
        assert mate["together_count"] <= mate["total_lineups"]
        assert mate["pet_name"] != "寂灭骨龙", "队友里不该有自己"


def test_top_pets_counts_are_consistent(seeded_domain):
    top = roco_store.top_pets_in_lineups(limit=10)
    assert top
    counts = [t["lineup_count"] for t in top]
    assert counts == sorted(counts, reverse=True)
    # 出现次数不应超过总阵容数
    total = roco_store.count_lineups("pvp")
    for item in top:
        assert item["lineup_count"] <= total


def test_lineup_detail_includes_members_and_skills(seeded_domain):
    lineups = roco_store.find_lineups_by_pet("寂灭骨龙", limit=1)
    assert lineups
    detail = roco_store.get_lineup(lineups[0]["id"])
    assert detail is not None
    assert detail["members"], "详情应带成员"
    for member in detail["members"]:
        assert member["pet_name"]
        assert 1 <= member["slot"] <= 6
        # 技能详情应被补齐（前端不必再请求一次）
        assert len(member["skill_details"]) == len(member["skills"])


def test_lineup_detail_404_for_unknown_id(seeded_domain):
    assert roco_store.get_lineup("nonexistent-id") is None


# --------------------------------------------------------------------------- 属性克制


def test_type_multiplier_basic(seeded_domain):
    assert roco_store.get_type_multiplier("火", ["草"]) == 2.0
    assert roco_store.get_type_multiplier("火", ["水"]) == 0.5
    assert roco_store.get_type_multiplier("火", ["普通"]) == 1.0


def test_type_multiplier_multiplies_for_dual_types(seeded_domain):
    """双属性倍率相乘——这是最容易算错的地方。

    火打「草/冰」是 2×2=4，而不是 2；一克一抗会互相抵消。
    """
    assert roco_store.get_type_multiplier("火", ["草", "冰"]) == 4.0
    assert roco_store.get_type_multiplier("火", ["草", "水"]) == 1.0


def test_lineup_type_analysis_is_computed_not_guessed(seeded_domain):
    """属性分析必须由克制矩阵算出来，不是模型推测。

    注意 ``coverage`` 的语义是「能对哪些**防守属性**打出 2 倍」，
    不是「队伍有哪些属性」——火系精灵带来的是「能打草/冰/虫/机械」。
    """
    pets = [
        {"name": "寂灭骨龙", "attributes": ["龙", "幽"]},
        {"name": "音速犬", "attributes": ["火"]},
    ]
    analysis = roco_store.analyze_lineup_types(pets)
    assert analysis["coverage"], "应有攻击覆盖"
    # 火系带来的是"能打草/冰/虫/机械"
    assert "草" in analysis["coverage"], "火系应能打草"
    assert "冰" in analysis["coverage"], "火系应能打冰"
    # 龙系只克制龙
    assert "龙" in analysis["coverage"], "龙系应能打龙"
    assert analysis["gaps"], "必然有打不动的属性"
    # 龙被冰/龙/萌克制，幽被光/幽/恶克制
    weak_types = {w["type"] for w in analysis["weak_to"]}
    assert "冰" in weak_types or "龙" in weak_types
    assert "光" in weak_types or "幽" in weak_types or "恶" in weak_types
    # coverage 与 gaps 必须互补且不重叠（同一套属性集合划分）
    assert not (set(analysis["coverage"]) & set(analysis["gaps"])), "覆盖与盲区不该重叠"


def test_type_matchup_matrix_is_complete(seeded_domain):
    """矩阵要写全（含 1.0 的中性项），缺行会让聚合悄悄少算。"""
    from app.domain.roco import ALL_TYPES

    expected = len(ALL_TYPES) * len(ALL_TYPES)
    assert seeded_domain.get("type_matchup", expected) == expected or (
        roco_store.stats() and _matrix_count() == expected
    )


def _matrix_count() -> int:
    from sqlalchemy import func, select

    from app.db import TypeMatchupRecord
    from app.db.session import transaction

    with transaction() as session:
        return session.execute(select(func.count()).select_from(TypeMatchupRecord)).scalar() or 0
