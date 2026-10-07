"""洛克王国检索层。

两个核心能力，分别对应两种查询：

1. **精确关系查询** —— 「哪几套阵容里有这只精灵」。
   走 ``lineup_members.pet_base_name`` 索引。这是向量检索做不到的：
   向量能告诉你"这段文字和这只精灵有关"，但数不出"有几套阵容"、
   也没法保证不漏。``find_lineups_by_pet`` 就是干这个的。

2. **属性分析** —— 给定一套阵容，算出它的攻防覆盖与弱点。
   走 ``type_matchup`` 矩阵，纯 SQL + 少量 Python 聚合。

名称归一化是这一层的关键细节：用户从图鉴选的是「化蝶」，
而阵容里可能写「化蝶（平常的样子）」。不做归一化就查不到，
而且**不报错**——只是返回空结果，看起来像"库里没有"。
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import Integer, func, literal, or_, select, Text

from app.db import LineupMemberRecord, LineupRecord, PetRecord, SkillRecord, TypeMatchupRecord
from app.db.session import transaction
from app.domain.roco import normalize_pet_name

logger = logging.getLogger(__name__)

# 反查时最多返回多少套阵容。玩家投稿总量约 150 套，
# 单只精灵通常出现在 10-40 套里，50 足够覆盖且不会撑爆上下文。
MAX_LINEUPS_PER_PET = 50


# --------------------------------------------------------------------------- 精灵 / 技能


def list_pets(
    keyword: str = "",
    attributes: list[str] | None = None,
    min_total: int = 0,
    sort: str = "no",
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """图鉴列表，支持**属性筛选与排序**。

    ``attributes`` 是**任一命中**（OR）而不是全部命中：用户勾「火 + 水」
    通常是想看这两系都有哪些精灵，而不是找同时是火水的双属性精灵
    （后者用 ``attributes_all`` 才合适，但那种精灵很少）。
    """
    stmt = select(PetRecord)
    if keyword:
        stmt = stmt.where(PetRecord.name.like(f"%{keyword.strip()}%"))
    if min_total > 0:
        # 种族值总和存在 JSON 里，用 json_extract 过滤。
        # SQLite 与 PostgreSQL 的语法不同，这里只支持 SQLite；
        # 换 PG 时改成 (stats->>'total')::int 即可。
        stmt = stmt.where(
            func.cast(func.json_extract(PetRecord.stats, "$.total"), Integer) >= min_total
        )
    if attributes:
        # **不能对 JSON 列做 LIKE 匹配属性名**：SQLAlchemy 把非 ASCII 存成
        # 转义形式（「火」存成 `["\u706b"]`），用 LIKE '%火%' 永远匹配不到，
        # 而且不报错——只是筛选结果恒为空，看起来像"这个属性没有精灵"。
        #
        # 改用预先算好的空格分隔列 ``attributes_text``（如 "光 龙"），
        # 配前后空格做**整词匹配**：'% 火 %' 不会误伤，且 SQLite 与
        # PostgreSQL 语法一致，不需要写两套 JSON 函数。
        clauses = [
            literal(" ").concat(PetRecord.attributes_text).concat(" ").like(f"% {a} %")
            for a in attributes
        ]
        stmt = stmt.where(or_(*clauses))

    stmt = _apply_pet_sort(stmt, sort)
    stmt = stmt.limit(limit).offset(offset)
    with transaction() as session:
        rows = session.execute(stmt).scalars().all()
    return [_pet_to_dict(r) for r in rows]


def _apply_pet_sort(stmt, sort: str):
    """排序。

    **必须带稳定的次级排序键**（no）：只按 total 排时，同分的精灵顺序
    由数据库决定，翻页会看到重复或漏项。
    """
    if sort == "total_desc":
        return stmt.order_by(
            func.cast(func.json_extract(PetRecord.stats, "$.total"), Integer).desc(),
            PetRecord.no.asc(),
        )
    if sort == "total_asc":
        return stmt.order_by(
            func.cast(func.json_extract(PetRecord.stats, "$.total"), Integer).asc(),
            PetRecord.no.asc(),
        )
    if sort == "speed_desc":
        return stmt.order_by(
            func.cast(func.json_extract(PetRecord.stats, "$.spd"), Integer).desc(),
            PetRecord.no.asc(),
        )
    if sort == "name":
        return stmt.order_by(PetRecord.name.asc(), PetRecord.no.asc())
    return stmt.order_by(PetRecord.no.asc(), PetRecord.form.asc())


def count_pets(
    keyword: str = "", attributes: list[str] | None = None, min_total: int = 0
) -> int:
    """与 ``list_pets`` 用同一套筛选条件计数，避免"显示 20 条但说共 465 只"。"""
    stmt = select(func.count()).select_from(PetRecord)
    if keyword:
        stmt = stmt.where(PetRecord.name.like(f"%{keyword.strip()}%"))
    if min_total > 0:
        stmt = stmt.where(
            func.cast(func.json_extract(PetRecord.stats, "$.total"), Integer) >= min_total
        )
    if attributes:
        clauses = [
            literal(" ").concat(PetRecord.attributes_text).concat(" ").like(f"% {a} %")
            for a in attributes
        ]
        stmt = stmt.where(or_(*clauses))
    with transaction() as session:
        return session.execute(stmt).scalar() or 0


def attribute_counts() -> list[dict]:
    """各属性有多少只精灵。前端筛选面板显示数量用。"""
    from app.domain.roco import ALL_TYPES

    with transaction() as session:
        rows = session.execute(select(PetRecord.attributes)).all()
    counter: Counter[str] = Counter()
    for (attrs,) in rows:
        for attr in attrs or []:
            counter[attr] += 1
    # 按 ALL_TYPES 的顺序返回，界面上顺序稳定
    return [
        {"type": t, "count": counter.get(t, 0)}
        for t in ALL_TYPES
        if counter.get(t, 0) > 0
    ]


def get_pet(name: str) -> dict | None:
    """按名字取精灵。

    先精确匹配基础名；匹配不到再尝试带形态的原文。
    返回第一个形态（多形态精灵的其它形态通过 ``forms`` 字段带出）。
    """
    base = normalize_pet_name(name)
    with transaction() as session:
        rows = (
            session.execute(
                select(PetRecord)
                .where(PetRecord.name == base)
                .order_by(PetRecord.no.asc())
            )
            .scalars()
            .all()
        )
        if not rows:
            return None
        data = _pet_to_dict(rows[0])
        data["forms"] = [r.form for r in rows if r.form]
        return data


def _pet_to_dict(record: PetRecord) -> dict:
    return {
        "id": record.id,
        "no": record.no,
        "name": record.name,
        "form": record.form,
        "attributes": record.attributes or [],
        "stats": record.stats or {},
        "ability": record.ability or {},
        "evolution_chain": record.evolution_chain,
        "has_shiny": record.has_shiny,
        "wiki_url": record.wiki_url,
        "image_url": record.image_url or "",
        "skill_names": record.skill_names or [],
        "source": record.source,
    }


def get_skills(names: list[str]) -> dict[str, dict]:
    """批量取技能详情，返回 ``{名字: 技能}``。

    批量而不是逐个查：一套阵容有 24 个技能（6 只 × 4），
    逐个查就是 24 次往返。
    """
    if not names:
        return {}
    unique = list({n for n in names if n})
    stmt = select(SkillRecord).where(SkillRecord.name.in_(unique))
    with transaction() as session:
        rows = session.execute(stmt).scalars().all()
    return {
        r.name: {
            "name": r.name,
            "attribute": r.attribute,
            "category": r.category,
            "power": r.power,
            "cost": r.cost,
            "description": r.description,
            "icon_url": r.icon_url or "",
        }
        for r in rows
    }


def list_skills(keyword: str = "", limit: int = 100) -> list[dict]:
    stmt = select(SkillRecord).order_by(SkillRecord.name.asc())
    if keyword:
        stmt = stmt.where(SkillRecord.name.like(f"%{keyword.strip()}%"))
    stmt = stmt.limit(limit)
    with transaction() as session:
        rows = session.execute(stmt).scalars().all()
    return [
        {
            "name": r.name,
            "attribute": r.attribute,
            "category": r.category,
            "power": r.power,
            "cost": r.cost,
            "description": r.description,
            "icon_url": r.icon_url or "",
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- 阵容反查（核心）


def find_lineups_by_pet(
    pet_name: str,
    lineup_type: str = "pvp",
    limit: int = MAX_LINEUPS_PER_PET,
) -> list[dict]:
    """**找出包含指定精灵的阵容。**

    这是「选精灵 → 查库里的阵容」的实现。走 ``pet_base_name`` 索引，
    精确命中，不依赖向量相似度。

    ``pet_name`` 会先归一化：用户选「化蝶」，库里可能存
    「化蝶（平常的样子）」，归一化后两者都能命中。
    """
    base = normalize_pet_name(pet_name)
    if not base:
        return []

    stmt = (
        select(LineupRecord)
        .join(LineupMemberRecord, LineupMemberRecord.lineup_id == LineupRecord.id)
        .where(LineupMemberRecord.pet_base_name == base)
        .order_by(LineupRecord.submitted_at.desc(), LineupRecord.insert_seq.asc())
        .limit(limit)
    )
    if lineup_type and lineup_type != "全部":
        stmt = stmt.where(LineupRecord.lineup_type == lineup_type)

    with transaction() as session:
        rows = session.execute(stmt).scalars().unique().all()
    return _attach_member_images([_lineup_to_dict(r) for r in rows])


def get_lineup(lineup_id: str) -> dict | None:
    """取一套阵容的完整信息（含 6 只成员与技能详情）。"""
    with transaction() as session:
        record = session.execute(
            select(LineupRecord).where(
                or_(LineupRecord.id == lineup_id, LineupRecord.wiki_id == lineup_id)
            )
        ).scalars().first()
        if record is None:
            return None
        members = (
            session.execute(
                select(LineupMemberRecord)
                .where(LineupMemberRecord.lineup_id == record.id)
                .order_by(LineupMemberRecord.slot.asc())
            )
            .scalars()
            .all()
        )

    data = _lineup_to_dict(record)
    skill_names = [s for m in members for s in (m.skills or [])]
    skill_detail = get_skills(skill_names)

    # 批量取成员精灵的属性与头像，避免逐只查（6 只就是 6 次往返）
    pet_info = _pet_brief_map([m.pet_base_name for m in members])

    data["members"] = [
        {
            "slot": m.slot,
            "pet_name": m.pet_name,
            "pet_base_name": m.pet_base_name,
            "bloodline": m.bloodline,
            "nature": m.nature,
            "talents": m.talents or [],
            "iv_config": m.iv_config or {},
            "skills": m.skills or [],
            # 精灵头像与属性：前端渲染成员卡片要用
            "image_url": (pet_info.get(m.pet_base_name) or {}).get("image_url", ""),
            "attributes": (pet_info.get(m.pet_base_name) or {}).get("attributes", []),
            "stats": (pet_info.get(m.pet_base_name) or {}).get("stats", {}),
            # 技能详情带上，前端不必再请求一次
            "skill_details": [
                skill_detail.get(s, {"name": s, "attribute": "", "category": "", "power": 0})
                for s in (m.skills or [])
            ],
        }
        for m in members
    ]
    return data


def _pet_brief_map(names: list[str]) -> dict[str, dict]:
    """按名字批量取精灵的简要信息（头像/属性/种族值）。

    **必须批量**：一套阵容 6 只，逐个 ``get_pet`` 就是 6 次查询；
    列表页一次渲染几十套阵容时会被放大成几百次。
    """
    unique = [n for n in {n for n in names if n}]
    if not unique:
        return {}
    with transaction() as session:
        rows = (
            session.execute(select(PetRecord).where(PetRecord.name.in_(unique)))
            .scalars()
            .all()
        )
    return {
        r.name: {
            "image_url": r.image_url or "",
            "attributes": r.attributes or [],
            "stats": r.stats or {},
        }
        for r in rows
    }


def _lineup_to_dict(record: LineupRecord) -> dict:
    return {
        "id": record.id,
        "wiki_id": record.wiki_id,
        "title": record.title,
        "lineup_type": record.lineup_type,
        "author": record.author,
        "intro": record.intro,
        "blood_magic": record.blood_magic,
        "magic_label": record.magic_label,
        "source_url": record.source_url,
        "source": record.source,
        "submitted_at": record.submitted_at,
        # 游戏内阵容码（可能为空串：只有 B 站源有）。前端给它一个复制按钮。
        "import_code": record.import_code or "",
        "member_names": record.member_names or [],
        # 卡片上要显示 6 个小头像，所以列表接口也要带图。
        # 由调用方通过 _attach_member_images 填充（一次批量查询）。
        "member_images": [],
    }


def _attach_member_images(lineups: list[dict]) -> list[dict]:
    """给一批阵容补上成员头像。

    **一次查询覆盖全部阵容的全部成员**，而不是每套阵容查一次——
    列表页一次返回 40 套就是 240 只精灵，逐个查会慢到不可用。
    """
    all_names: list[str] = []
    for lineup in lineups:
        all_names.extend(lineup.get("member_names") or [])
    if not all_names:
        return lineups

    info = _pet_brief_map(all_names)
    for lineup in lineups:
        lineup["member_images"] = [
            {
                "name": name,
                "image_url": (info.get(name) or {}).get("image_url", ""),
                "attributes": (info.get(name) or {}).get("attributes", []),
            }
            for name in (lineup.get("member_names") or [])
        ]
    return lineups


def list_lineups(
    lineup_type: str = "pvp",
    keyword: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    stmt = select(LineupRecord).order_by(
        LineupRecord.submitted_at.desc(), LineupRecord.insert_seq.asc()
    )
    if lineup_type and lineup_type != "全部":
        stmt = stmt.where(LineupRecord.lineup_type == lineup_type)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(LineupRecord.title.like(pattern), LineupRecord.intro.like(pattern))
        )
    stmt = stmt.limit(limit).offset(offset)
    with transaction() as session:
        rows = session.execute(stmt).scalars().all()
    return _attach_member_images([_lineup_to_dict(r) for r in rows])


def count_lineups(lineup_type: str = "pvp") -> int:
    stmt = select(func.count()).select_from(LineupRecord)
    if lineup_type and lineup_type != "全部":
        stmt = stmt.where(LineupRecord.lineup_type == lineup_type)
    with transaction() as session:
        return session.execute(stmt).scalar() or 0


def top_pets_in_lineups(limit: int = 20, lineup_type: str = "pvp") -> list[dict]:
    """出场次数最多的精灵。

    ⚠️ 这是**玩家投稿里的出现次数**，不是使用率或胜率。
    玩家投稿不能代表对局分布，所以界面上必须标注清楚，
    不能把它包装成「版本强势精灵排行榜」。
    """
    stmt = (
        select(
            LineupMemberRecord.pet_base_name,
            func.count(func.distinct(LineupMemberRecord.lineup_id)).label("lineup_count"),
        )
        .join(LineupRecord, LineupRecord.id == LineupMemberRecord.lineup_id)
        .group_by(LineupMemberRecord.pet_base_name)
        .order_by(func.count(func.distinct(LineupMemberRecord.lineup_id)).desc())
        .limit(limit)
    )
    if lineup_type and lineup_type != "全部":
        stmt = stmt.where(LineupRecord.lineup_type == lineup_type)

    with transaction() as session:
        rows = session.execute(stmt).all()
    return [
        {"pet_name": r.pet_base_name, "lineup_count": int(r.lineup_count)} for r in rows
    ]


def teammates_of(pet_name: str, lineup_type: str = "pvp", limit: int = 12) -> list[dict]:
    """和这只精灵**同队出场最多**的其它精灵。

    这是「推荐阵容」的**数据依据**：与其让模型凭空想搭配，
    不如先把真实投稿里和它同队过的精灵统计出来给它看。
    模型据此推荐，结论就有出处。
    """
    base = normalize_pet_name(pet_name)
    if not base:
        return []

    with transaction() as session:
        # 先找出包含该精灵的阵容 id
        lineup_ids = [
            row[0]
            for row in session.execute(
                select(LineupMemberRecord.lineup_id)
                .where(LineupMemberRecord.pet_base_name == base)
                .distinct()
            ).all()
        ]
        if not lineup_ids:
            return []

        # 再统计这些阵容里其它精灵的出现次数
        stmt = (
            select(
                LineupMemberRecord.pet_base_name,
                func.count(func.distinct(LineupMemberRecord.lineup_id)).label("together"),
            )
            .where(
                LineupMemberRecord.lineup_id.in_(lineup_ids),
                LineupMemberRecord.pet_base_name != base,
            )
            .group_by(LineupMemberRecord.pet_base_name)
            .order_by(func.count(func.distinct(LineupMemberRecord.lineup_id)).desc())
            .limit(limit)
        )
        rows = session.execute(stmt).all()

    return [
        {
            "pet_name": r.pet_base_name,
            "together_count": int(r.together),
            "total_lineups": len(lineup_ids),
        }
        for r in rows
    ]


# --------------------------------------------------------------------------- 属性分析


def get_type_multiplier(attack_type: str, defend_types: list[str]) -> float:
    """算一次攻击对多属性目标的倍率（相乘）。

    双属性精灵被两种属性同时克制时是 4 倍，一克一抗是 1 倍——
    这个相乘逻辑必须和游戏一致，否则推荐出来的"克制关系"是错的。
    """
    if not defend_types:
        return 1.0
    with transaction() as session:
        rows = (
            session.execute(
                select(TypeMatchupRecord).where(
                    TypeMatchupRecord.attack_type == attack_type,
                    TypeMatchupRecord.defend_type.in_(defend_types),
                )
            )
            .scalars()
            .all()
        )
    multipliers = {r.defend_type: r.multiplier for r in rows}
    result = 1.0
    for dtype in defend_types:
        result *= multipliers.get(dtype, 1.0)
    return round(result, 3)


def analyze_lineup_types(member_pets: list[dict]) -> dict:
    """分析一套阵容的属性攻防。

    ``member_pets`` 是 ``[{"name":..., "attributes":[...]}, ...]``。

    返回（注意四项的语义各不相同，容易混）：

    - ``weak_to``：这套阵容**被打**时，哪些属性克制它（按命中数排序）——最大风险
    - ``resist``：这套阵容**被打**时，能抵抗哪些属性
    - ``coverage``：这套阵容**打别人**时，能对哪些防守属性打出 2 倍。
      注意是**防守方属性**，不是"队伍有哪些属性"——火系精灵带来的是
      「能打草/冰/虫/机械」，而不是「能打火」。
    - ``gaps``：打不出 2 倍的防守属性（攻击盲区）。
      属性集合对攻防是同一套，所以 ``全部属性 - coverage`` 就是盲区。
    """
    weak_counter: Counter[str] = Counter()
    resist_counter: Counter[str] = Counter()
    coverage: set[str] = set()

    with transaction() as session:
        # 一次取出全矩阵，避免逐个组合查询
        rows = session.execute(select(TypeMatchupRecord)).scalars().all()

    strong: dict[str, set[str]] = {}
    weak: dict[str, set[str]] = {}
    resists: dict[str, set[str]] = {}
    for row in rows:
        if row.multiplier >= 2.0:
            strong.setdefault(row.attack_type, set()).add(row.defend_type)
            weak.setdefault(row.defend_type, set()).add(row.attack_type)
        elif row.multiplier <= 0.5:
            resists.setdefault(row.defend_type, set()).add(row.attack_type)

    for pet in member_pets:
        attrs = pet.get("attributes") or []
        for attr in attrs:
            coverage |= strong.get(attr, set())
            for attacker in weak.get(attr, set()):
                weak_counter[attacker] += 1
            for attacker in resists.get(attr, set()):
                resist_counter[attacker] += 1

    all_types = set(strong.keys())
    return {
        "weak_to": [{"type": t, "count": c} for t, c in weak_counter.most_common()],
        "resist": [{"type": t, "count": c} for t, c in resist_counter.most_common()],
        "coverage": sorted(coverage),
        # 打不动的属性：去掉「无」（它本就不参与克制）
        "gaps": sorted(all_types - coverage - {"无"}),
    }


def stats() -> dict:
    """检索层的数据概览，供健康检查与前端展示。"""
    return {
        "pets": count_pets(),
        "lineups_pvp": count_lineups("pvp"),
        "lineups_pve": count_lineups("pve"),
        "lineups_total": count_lineups("全部"),
    }
