"""把种子数据导入领域表。

数据来源与许可（**必须保留这段说明**）
--------------------------------------
- ``pvp_lineups_mit.json`` —— 来自 [justeHe/roco-battle-simulator](https://github.com/justeHe/roco-battle-simulator)，
  **MIT 许可**。实测它是阵容数据的**超集**：另一个来源的 148 套 PvP 阵容
  **全部**包含在它的 151 套里（交集 148、并集 151），所以只用这一份即可，
  既拿到最全的数据，又只依赖许可最干净的那个仓库。
  它还独有数值化个体值（``iv_config``），另一个来源只有方向没有数值。
- ``sprites_rocom.json`` / ``skills_rocom.csv`` —— 来自
  [AofeiLi-code/rocom-data](https://github.com/AofeiLi-code/rocom-data)。
  ⚠️ **该仓库没有 LICENSE 文件**（GitHub API 的 ``license`` 字段为 null），
  作者 README 声明「禁止商业使用」。这里只取**图鉴类事实数据**（精灵属性、
  种族值、技能数值），**不取它的阵容数据**，且项目整体按非商业用途交付。
  若要商用，请自行替换这份数据。

关于时效
--------
阵容是**玩家投稿**，不是胜率统计。``notice`` 字段明确写着
「数据不代表对局使用率」。所以系统**不能**说「这是强势阵容」，
只能说「有人这么配过」——这一点在提示词里也写死了。

游戏当前为 S4 赛季（2026-09-10 开启），种子数据停留在 2026-05 前后，
**会落后于赛季**。要新数据必须自己跑爬虫（``app.scraper``）。
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from sqlalchemy import delete, func, select

from app.db import LineupMemberRecord, LineupRecord, PetRecord, SkillRecord, TypeMatchupRecord
from app.db.session import transaction
from app.domain.roco import (
    ALL_TYPES,
    TYPE_MATCHUP,
    normalize_pet_name,
    split_form,
)

logger = logging.getLogger(__name__)

SEED_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "_seed"

LINEUPS_MIT = "pvp_lineups_mit.json"
SPRITES = "sprites_rocom.json"
SKILLS = "skills_rocom.csv"


class SeedError(RuntimeError):
    """种子导入失败，消息可直接展示。"""


def _load_json(path: Path):
    if not path.exists():
        raise SeedError(f"种子文件不存在：{path}")
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------------- 属性克制


def seed_type_matchup() -> int:
    """把 domain 里的克制表物化成 19×19 矩阵。

    **写全矩阵**（含 1.0 的中性项），而不是只写非 1 的项：
    查询「这套阵容对火系的防御表现」时需要按 defend_type 取全部攻击属性，
    缺行会让聚合结果悄悄少算。
    """
    rows = 0
    with transaction() as session:
        session.execute(delete(TypeMatchupRecord))
        for attack in ALL_TYPES:
            relation = TYPE_MATCHUP.get(attack, {})
            strong = set(relation.get("strong_against", []))
            weak = set(relation.get("resisted_by", []))
            for defend in ALL_TYPES:
                if defend in strong:
                    multiplier = 2.0
                elif defend in weak:
                    multiplier = 0.5
                else:
                    multiplier = 1.0
                session.add(
                    TypeMatchupRecord(
                        attack_type=attack, defend_type=defend, multiplier=multiplier
                    )
                )
                rows += 1
    logger.info("属性克制矩阵：%d 行", rows)
    return rows


# --------------------------------------------------------------------------- 精灵


def seed_pets() -> int:
    sprites = _load_json(SEED_DIR / SPRITES)
    if not isinstance(sprites, list):
        raise SeedError(f"{SPRITES} 期望是数组，实际是 {type(sprites).__name__}")

    # 头像 URL 来自 images 同步的缓存；没同步过就是空，前端显示占位图。
    # **不让 seed 依赖联网**——同步是单独的可选步骤。
    from app.scraper.images import PET_IMAGE_CACHE, load_cached

    image_map = load_cached(PET_IMAGE_CACHE)
    # 归一化键：图鉴页用带形态的全名，本地 sprites 也用全名，但两边
    # 括号写法可能不同，所以同时按基础名兜底查一次。
    normalized_images: dict[str, str] = {}
    for key, url in image_map.items():
        normalized_images.setdefault(normalize_pet_name(key), url)

    count = 0
    with transaction() as session:
        session.execute(delete(PetRecord))
        for index, item in enumerate(sprites):
            name = (item.get("name") or "").strip()
            if not name:
                continue
            base, form = split_form(name)
            image_url = (
                image_map.get(name)
                or normalized_images.get(normalize_pet_name(name))
                or ""
            )
            session.add(
                PetRecord(
                    id=f"pet-{item.get('no', 0)}-{index}",
                    insert_seq=index + 1,
                    no=int(item.get("no") or 0),
                    name=base,
                    form=form or (item.get("form") or ""),
                    attributes=item.get("attributes") or [],
                    attributes_text=_attr_text(item.get("attributes")),
                    stats=item.get("stats") or {},
                    ability=item.get("ability") or {},
                    evolution_chain=item.get("evolution_chain"),
                    has_shiny=bool(item.get("has_shiny")),
                    wiki_url=item.get("url") or "",
                    image_url=image_url,
                    skill_names=[
                        s.get("name")
                        for s in (item.get("skills") or [])
                        if isinstance(s, dict) and s.get("name")
                    ],
                    source="rocom-data/sprites.json",
                )
            )
            count += 1
    logger.info("精灵图鉴：%d 条（带头像 %d 条）", count, sum(1 for s in sprites if s.get("name") in image_map))
    return count


# --------------------------------------------------------------------------- 技能


def seed_skills() -> int:
    """技能优先从 sprites.json 内嵌的技能取（信息更全，含等级与分类），
    CSV 作为补充。

    为什么要合并两个来源：sprites.json 里的技能带 ``level``（学习等级），
    而 CSV 是独立技能表、覆盖面可能更广。只取一个都会漏。
    """
    skills: dict[str, dict] = {}

    # 技能图标 URL 同样来自 images 同步的缓存；没同步过就是空串。
    from app.scraper.images import SKILL_ICON_CACHE, load_cached

    icon_map = load_cached(SKILL_ICON_CACHE)

    sprites = _load_json(SEED_DIR / SPRITES)
    for sprite in sprites:
        for skill in sprite.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            name = (skill.get("name") or "").strip()
            if not name:
                continue
            skills.setdefault(
                name,
                {
                    "name": name,
                    "attribute": skill.get("attribute") or "",
                    "category": skill.get("category") or "",
                    "power": _to_int(skill.get("power")),
                    "cost": _to_int(skill.get("cost")),
                    "description": skill.get("description") or "",
                },
            )

    csv_path = SEED_DIR / SKILLS
    if csv_path.exists():
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                name = (row.get("技能名") or "").strip()
                if not name:
                    continue
                existing = skills.get(name)
                if existing:
                    # CSV 补齐空字段，不覆盖已有值
                    existing["attribute"] = existing["attribute"] or (row.get("属性") or "")
                    existing["category"] = existing["category"] or (row.get("类型") or "")
                    existing["power"] = existing["power"] or _to_int(row.get("威力"))
                    existing["cost"] = existing["cost"] or _to_int(row.get("耗能"))
                    existing["description"] = existing["description"] or (
                        row.get("效果描述") or ""
                    )
                else:
                    skills[name] = {
                        "name": name,
                        "attribute": row.get("属性") or "",
                        "category": row.get("类型") or "",
                        "power": _to_int(row.get("威力")),
                        "cost": _to_int(row.get("耗能")),
                        "description": row.get("效果描述") or "",
                    }

    with transaction() as session:
        session.execute(delete(SkillRecord))
        for index, skill in enumerate(skills.values()):
            session.add(
                SkillRecord(
                    id=f"skill-{index}",
                    insert_seq=index + 1,
                    name=skill["name"],
                    attribute=skill["attribute"],
                    category=skill["category"],
                    power=skill["power"],
                    cost=skill["cost"],
                    description=skill["description"],
                    icon_url=icon_map.get(skill["name"], ""),
                    source="rocom-data/sprites+skills.csv",
                )
            )
    logger.info("技能图鉴：%d 条（带图标 %d 条）", len(skills), sum(1 for n in skills if n in icon_map))
    return len(skills)


def _attr_text(attributes) -> str:
    """把属性数组拼成空格分隔的可搜索文本。

    与 ``attributes`` JSON 列**必须同步维护**——它是筛选的实际依据，
    不同步会导致"界面上看得到属性、却筛不出来"。
    """
    return " ".join(a for a in (attributes or []) if a)


def _to_int(value) -> int:
    try:
        return int(str(value).strip() or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- 阵容


def seed_lineups() -> tuple[int, int]:
    """导入阵容与成员。返回 ``(阵容数, 成员数)``。"""
    payload = _load_json(SEED_DIR / LINEUPS_MIT)
    lineups = payload.get("lineups") if isinstance(payload, dict) else payload
    if not lineups:
        raise SeedError(f"{LINEUPS_MIT} 里没有 lineups 数组")

    lineup_count = 0
    member_count = 0

    with transaction() as session:
        # 成员随外键级联删除，所以删阵容就够了
        session.execute(delete(LineupRecord))

        for index, item in enumerate(lineups):
            wiki_id = (item.get("id") or "").strip()
            if not wiki_id:
                continue

            members = item.get("members") or []
            names: list[str] = []
            member_rows: list[LineupMemberRecord] = []

            for member in members:
                raw_name = (member.get("name") or member.get("card_name") or "").strip()
                if not raw_name:
                    continue
                base = normalize_pet_name(raw_name)
                names.append(base)
                member_rows.append(
                    LineupMemberRecord(
                        lineup_id=wiki_id,
                        slot=int(member.get("slot") or len(member_rows) + 1),
                        pet_name=raw_name,
                        pet_base_name=base,
                        bloodline=(member.get("bloodline") or "").strip(),
                        nature=(member.get("nature") or "").strip(),
                        talents=member.get("iv_names") or [],
                        iv_config=member.get("iv_config") or {},
                        skills=member.get("skills") or [],
                    )
                )

            if not member_rows:
                continue

            session.add(
                LineupRecord(
                    id=wiki_id,
                    insert_seq=index + 1,
                    wiki_id=wiki_id,
                    title=(item.get("name") or item.get("title") or "未命名阵容").strip(),
                    lineup_type=(item.get("type") or "pvp").strip().lower(),
                    author=(item.get("author") or "").strip(),
                    intro=(item.get("description") or item.get("intro") or "").strip(),
                    blood_magic=(item.get("magic") or item.get("blood_magic") or "").strip(),
                    magic_label=(item.get("magic_label") or "").strip(),
                    source_url=(item.get("source_url") or "").strip(),
                    source="justeHe/roco-battle-simulator (MIT)",
                    submitted_at=(item.get("updated_at") or "").strip(),
                    member_names=names,
                )
            )
            lineup_count += 1

            # **必须先 flush 主行再插成员**。
            # SQLAlchemy 的 unit of work 只看 ORM 关系来排插入顺序，而这里
            # 成员是「裸外键」（没有 relationship() 声明），它不知道依赖关系，
            # 可能先插成员 -> 撞 FOREIGN KEY constraint failed。
            # flush 一次把父行落库，顺序就确定了。
            session.flush()
            for row in member_rows:
                session.add(row)
                member_count += 1

    logger.info("阵容：%d 套 / %d 个成员槽位", lineup_count, member_count)
    return lineup_count, member_count


# --------------------------------------------------------------------------- 总入口


def seed_all() -> dict:
    """导入全部种子数据。幂等——每次先清表再写。"""
    stats = {
        "type_matchup": seed_type_matchup(),
        "pets": seed_pets(),
        "skills": seed_skills(),
    }
    lineups, members = seed_lineups()
    stats["lineups"] = lineups
    stats["lineup_members"] = members
    return stats


def seed_status() -> dict:
    """当前库里各表行数，供健康检查/管理页展示。"""
    with transaction() as session:
        return {
            "pets": session.execute(select(func.count()).select_from(PetRecord)).scalar() or 0,
            "skills": session.execute(select(func.count()).select_from(SkillRecord)).scalar() or 0,
            "lineups": session.execute(select(func.count()).select_from(LineupRecord)).scalar() or 0,
            "lineup_members": session.execute(
                select(func.count()).select_from(LineupMemberRecord)
            ).scalar()
            or 0,
            "type_matchup": session.execute(
                select(func.count()).select_from(TypeMatchupRecord)
            ).scalar()
            or 0,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    from app.db.session import init_db

    init_db()
    print(seed_all())
