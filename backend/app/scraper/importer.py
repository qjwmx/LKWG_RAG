"""把爬虫缓存导入领域表。

为什么单独一个模块
------------------
原来这段逻辑写死在 ``run_scrape._import_cache`` 里，只认 BWIKI 一种
缓存格式、``source`` 字段也硬编码成 BWIKI。加入第二个数据源后，
继续硬编码会让「B 站抓的阵容被标成来自 BWIKI」——来源标注错误比
数据缺失更糟，因为使用者无法判断该不该信。

所以这里做**源无关的导入**：各爬虫只要产出统一的字段约定，
导入逻辑与来源无关，``source`` / ``source_url`` 由缓存项自带。

统一字段约定（各爬虫的 ``lineups.json`` 里每条）
------------------------------------------------
::

    {
      "wiki_id": "去重主键，必须非空且稳定",
      "title": "阵容标题",
      "lineup_type": "pvp" / "pve",
      "author": "投稿人",
      "intro": "阵容介绍",
      "blood_magic": "血脉魔法",
      "submitted_at": "2026-6-9（文本，写法不统一）",
      "source": "数据来源说明（会展示给用户）",
      "source_url": "原始页面 URL",
      "members": [{"slot":1,"name":"..","bloodline":"..",
                   "nature":"..","talents":[],"skills":[...]}]
    }

增量语义
--------
按 ``wiki_id`` 去重：已存在则更新元信息 + 重建成员（成员数可能变），
不存在则插入。**不删**任何已有行——导入是增量的，删数据必须是显式的
另一个动作。

**成员字段必须做合并，不能整体覆盖**
------------------------------------
不同数据源覆盖的字段不同：种子数据（``justeHe/roco-battle-simulator``）
带**数值化个体值** ``iv_config``（``{"hp":60,"atk":0,...}``），
而 BWIKI 爬虫只拿得到方向（``talents=["生命","魔攻"]``），
``iv_config`` 是空的。

第一版导入"删掉旧成员再插新成员"，结果 **150 套阵容的 906 条成员
个体值全被清空**——而且**不报错**：阵容数、成员数、接口返回全都正常，
只有前端那张个体值图变成空的。这类"字段静默丢失"最难发现。

所以现在按 ``(slot, 归一化精灵名)`` 对齐新旧成员，新数据为空的字段
从旧数据继承；并且**继承了多少字段会记进来源标注**，
免得使用者以为数据全来自爬虫。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from sqlalchemy import select

from app.db import LineupMemberRecord, LineupRecord
from app.db.session import transaction
from app.domain.roco import LINEUP_TYPE_PVE, LINEUP_TYPE_PVP, normalize_pet_name

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "_scrape_cache"

# 各数据源的缓存文件。键是 ``--source`` 的值。
SOURCE_CACHE = {
    "bwiki": "lineups.json",
    "bilibili": "lineups_bilibili.json",
}

# 技能字段里的模板占位（`文件:技能图标 .png`）。与 bwiki._SKILL_NOISE_RE
# 同一个判定，重复一份是刻意的：导入层不能依赖某个爬虫模块被 import 过。
_SKILL_NOISE_RE = re.compile(r"(文件:|\.png|\.jpg|\.jpeg|\.gif|\[\[|\]\])", re.I)


class ImportError_(RuntimeError):
    """导入失败，消息可直接展示。"""


def _load_items(path: Path) -> list[dict]:
    """读缓存，兼容两种形状：裸数组，或 ``{"lineups": [...]}``。

    B 站缓存用后者（还要额外存已抓过的 aid 列表做续传），
    BWIKI 用前者。导入层统一处理，免得每个爬虫都要迁就旧格式。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ImportError_(f"缓存读取失败：{path}\n{exc}") from exc

    if isinstance(payload, dict):
        items = payload.get("lineups") or []
    elif isinstance(payload, list):
        items = payload
    else:
        raise ImportError_(f"缓存格式不认识（{type(payload).__name__}）：{path}")

    if not isinstance(items, list):
        raise ImportError_(f"lineups 期望是数组，实际是 {type(items).__name__}：{path}")
    return items


def _clean_type(value: str) -> str:
    """规范化阵容类型。

    ``lineups`` 表有 ``CHECK (lineup_type IN ('pvp','pve'))`` 约束，
    写入非法值会撞约束报错。爬来的数据里可能是 ``PVP`` / ``pvp `` /
    空串 / ``pve副本``，统一收敛，兜底 pvp（玩家投稿绝大多数是 pvp）。
    """
    text = (value or "").strip().lower()
    if LINEUP_TYPE_PVE in text:
        return LINEUP_TYPE_PVE
    if LINEUP_TYPE_PVP in text:
        return LINEUP_TYPE_PVP
    return LINEUP_TYPE_PVP


def _merge_sources(old: str, new: str) -> str:
    """合并来源标注，避免覆盖掉原始出处。

    为什么需要：150 套种子阵容（MIT 数据源，带数值化个体值）会被
    BWIKI 爬虫以同一个 ``wiki_id`` 更新。若直接写 ``record.source = 新源``，
    那 906 条个体值的**真实出处就消失了**——使用者会以为数据全来自爬虫，
    而 MIT 那份的署名要求（以及"个体值从哪来"的可追溯性）就断了。

    所以两个来源不同就并列保留。用 `` + `` 分隔，便于前端展示与人工阅读。
    """
    old, new = (old or "").strip(), (new or "").strip()
    if not old:
        return new
    if not new or old == new:
        return old
    # 已经并列过的不要重复追加（重复导入是常态）
    if new in old:
        return old
    return f"{old} + {new}"


def import_lineups(items: list[dict], default_source: str) -> tuple[int, int, int]:
    """把阵容写进库。返回 ``(新增, 更新, 跳过)``。

    ``default_source`` 只在缓存项没带 ``source`` 时用；正常情况下
    每条都自带来源，因为**来源必须能追到具体站点**。
    """
    inserted = updated = skipped = 0
    inherited = 0

    with transaction() as session:
        # 预取已存在的 id，避免逐条 session.get 的往返
        existing_ids = {
            row for row in session.execute(select(LineupRecord.id)).scalars()
        }

        for item in items:
            wiki_id = (item.get("wiki_id") or "").strip()
            if not wiki_id:
                skipped += 1
                continue

            members = item.get("members") or []
            if not members:
                skipped += 1
                continue

            # 成员名字去重但保持顺序；空名跳过
            names: list[str] = []
            for member in members:
                raw = (member.get("name") or "").strip()
                if raw:
                    names.append(normalize_pet_name(raw))
            if not names:
                skipped += 1
                continue

            source = (item.get("source") or default_source).strip() or default_source

            # 旧成员：按 (slot, 归一化名) 建索引，用于继承新数据没有的字段。
            # **必须在 delete 之前把值快照成普通 dict**：先 bulk delete 再读
            # 已删除的 ORM 对象，值可能已被过期/回收，继承会静默失效。
            old_by_key: dict[tuple[int, str], dict] = {}
            old_source = ""
            if wiki_id in existing_ids:
                existing_record = session.get(LineupRecord, wiki_id)
                if existing_record is not None:
                    old_source = existing_record.source or ""
                for old in session.execute(
                    select(LineupMemberRecord).where(
                        LineupMemberRecord.lineup_id == wiki_id
                    )
                ).scalars():
                    old_by_key[(old.slot, old.pet_base_name)] = {
                        "iv_config": dict(old.iv_config or {}),
                        "talents": list(old.talents or []),
                        "nature": old.nature or "",
                        "bloodline": old.bloodline or "",
                    }

            if wiki_id in existing_ids:
                record = session.get(LineupRecord, wiki_id)
                if record is None:  # 竞态兜底
                    skipped += 1
                    continue
                record.title = item.get("title") or record.title
                record.intro = item.get("intro") or ""
                record.author = item.get("author") or ""
                record.blood_magic = item.get("blood_magic") or ""
                record.submitted_at = item.get("submitted_at") or ""
                record.lineup_type = _clean_type(item.get("lineup_type"))
                record.source = _merge_sources(old_source, source)
                record.source_url = item.get("source_url") or record.source_url
                record.member_names = names
                # 阵容码：新数据没有就保留旧的（B 站源独有，别被别的源清掉）
                record.import_code = item.get("import_code") or record.import_code
                # 成员重建：成员数可能变，逐条 diff 更复杂且没收益。
                # 但**重建前先把旧值按 key 取出来**，下面逐个字段合并。
                session.query(LineupMemberRecord).filter(
                    LineupMemberRecord.lineup_id == wiki_id
                ).delete()
                updated += 1
            else:
                from app.services.storage import _next_seq

                session.add(
                    LineupRecord(
                        id=wiki_id,
                        insert_seq=_next_seq(session, LineupRecord),
                        wiki_id=wiki_id,
                        title=item.get("title") or "未命名阵容",
                        lineup_type=_clean_type(item.get("lineup_type")),
                        author=item.get("author") or "",
                        intro=item.get("intro") or "",
                        blood_magic=item.get("blood_magic") or "",
                        source_url=item.get("source_url") or "",
                        source=source,
                        submitted_at=item.get("submitted_at") or "",
                        import_code=item.get("import_code") or "",
                        member_names=names,
                    )
                )
                existing_ids.add(wiki_id)
                # **必须先 flush 主行**：成员是裸外键（没有 relationship()），
                # SQLAlchemy 的 unit of work 不知道依赖顺序，可能先插成员
                # 而撞 FOREIGN KEY constraint failed。
                session.flush()
                inserted += 1

            for index, member in enumerate(members, 1):
                raw_name = (member.get("name") or "").strip()
                if not raw_name:
                    continue
                base_name = normalize_pet_name(raw_name)
                slot = int(member.get("slot") or index)

                # 新数据缺失的字段从旧成员继承（见模块 docstring 的说明）。
                # 只继承**新数据为空**的字段，不覆盖已有值。
                old = old_by_key.get((slot, base_name))
                iv_config = member.get("iv_config") or {}
                talents = member.get("talents") or []
                nature = member.get("nature") or ""
                bloodline = member.get("bloodline") or ""
                if old is not None:
                    if not iv_config and old["iv_config"]:
                        iv_config = old["iv_config"]
                        inherited += 1
                    if not talents and old["talents"]:
                        talents = old["talents"]
                    if not nature and old["nature"]:
                        nature = old["nature"]
                    if not bloodline and old["bloodline"]:
                        bloodline = old["bloodline"]

                # 技能名做一次兜底清洗：模板占位（`文件:…png`）会混进来，
                # 而它长得像文本、能一路进库并进提示词。爬虫层已经过滤，
                # 这里再挡一次是因为**旧缓存文件仍然存在**——
                # 重新导入一份没重抓的缓存时不该把垃圾带进库。
                skills = [
                    s.strip()
                    for s in (member.get("skills") or [])
                    if s and s.strip() and not _SKILL_NOISE_RE.search(s)
                ]

                session.add(
                    LineupMemberRecord(
                        lineup_id=wiki_id,
                        slot=slot,
                        pet_name=raw_name,
                        pet_base_name=base_name,
                        bloodline=bloodline,
                        nature=nature,
                        talents=talents,
                        iv_config=iv_config,
                        skills=skills,
                    )
                )

    logger.info(
        "导入完成：新增 %d，更新 %d，跳过 %d，继承个体值 %d 条",
        inserted, updated, skipped, inherited,
    )
    return inserted, updated, skipped


def import_source(source: str) -> tuple[int, int, int]:
    """按数据源名导入它的缓存。``source`` 取 ``SOURCE_CACHE`` 的键。"""
    cache_name = SOURCE_CACHE.get(source)
    if cache_name is None:
        raise ImportError_(
            f"未知数据源：{source}（可用：{', '.join(sorted(SOURCE_CACHE))}）"
        )

    path = CACHE_DIR / cache_name
    if not path.exists():
        raise ImportError_(f"缓存不存在：{path}\n先跑一次对应的抓取。")

    items = _load_items(path)
    if not items:
        raise ImportError_(f"缓存里没有阵容：{path}")

    default_source = {
        "bwiki": "bwiki（自建爬虫）",
        "bilibili": "bilibili 专栏（自建爬虫）",
    }.get(source, source)

    logger.info("从 %s 导入 %d 套阵容", path.name, len(items))
    return import_lineups(items, default_source=default_source)
