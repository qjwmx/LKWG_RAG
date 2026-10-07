"""洛克王国领域表：精灵、技能、阵容。

为什么不能只用现有的 ``documents`` / ``document_chunks``
--------------------------------------------------------
那两张表是**非结构化**的：它们能回答"关于 X 的文档里说了什么"，
但回答不了"**哪几套阵容里有这只精灵**"。后者是精确的关系查询，
需要 ``lineup_members(pet_name)`` 这样的索引。

所以这里做**结构化 + 向量并存**：
- 精确问题（含某精灵的阵容、某技能的威力）走 SQL；
- 模糊问题（什么阵容克制水系）走向量检索 ``document_chunks``。

写入时两边都灌：``lineups`` 落结构化行，同时把每套阵容渲染成一段文本
灌进 ``documents``/``document_chunks``。**这不是冗余**——它们服务不同的查询模式。

命名沿用既有约定：实体加 ``Record`` 后缀，避免与 LangChain / 通用名字撞车。
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base, _seq_column


def _now_column():
    return mapped_column(TIMESTAMP, nullable=False, server_default=func.now())


class PetRecord(Base):
    """精灵图鉴。

    ``stats`` 存六维种族值 JSON，``attributes`` 存主/副属性数组——
    它们只用于展示与计算，不做查询条件，所以不拆成列。
    真正需要索引的是 ``name``（反查阵容时 JOIN 用）。
    """

    __tablename__ = "pets"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    insert_seq: Mapped[int] = _seq_column()
    # 图鉴号。同一个 no 可能有多个形态（form 不同）。
    no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # 形态描述（「平常的样子」）。无形态时为空串。
    # 与 name 分开存，这样「化蝶」的 4 个形态不会在按名字查询时互相混淆。
    form: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attributes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 属性的**可搜索副本**：空格分隔，如 "光 龙"。
    #
    # 为什么冗余一份：JSON 列里非 ASCII 会被存成转义形式（「火」-> `\u706b`），
    # 对 JSON 列做 LIKE '%火%' 永远匹配不到且不报错——筛选结果恒为空，
    # 排查起来像"这个属性没有精灵"。而 JSON 展开函数（json_each /
    # jsonb_array_elements）在 SQLite 与 PostgreSQL 上语法不同，
    # 会让筛选逻辑绑死数据库。
    #
    # 存一份空格分隔的文本 + 前后补空格做整词匹配，两种库都能用，
    # 且索引友好。写入时由 seed_loader 保证与 attributes 同步。
    attributes_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    stats: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    ability: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # 进化链：[[{no,name,level}], ...]，可能为 null
    evolution_chain: Mapped[list | None] = mapped_column(JSON, nullable=True)
    has_shiny: Mapped[bool] = mapped_column(nullable=False, default=False)
    wiki_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 头像 URL（BWIKI patchwiki 热链）。空串表示没有图，前端显示占位。
    image_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 该精灵可学技能名列表（冗余但省一次 JOIN；技能详情在 skills 表）
    skill_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 数据来源与时效。**必须标注**——玩家数据会随赛季过期，
    # 不标来源的话使用者无法判断该不该信。
    source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        UniqueConstraint("name", "form", name="pets_name_form_key"),
        Index("pets_name_idx", "name"),
        Index("pets_no_idx", "no"),
    )


class SkillRecord(Base):
    """技能图鉴。"""

    __tablename__ = "skills"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    insert_seq: Mapped[int] = _seq_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    attribute: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 物攻 / 魔攻 / 防御 / 状态 / 特性
    category: Mapped[str] = mapped_column(Text, nullable=False, default="")
    power: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 技能图标 URL。特性与普通技能的文件名前缀不同（Feature_ / Skill_），
    # 由 scraper.images 在同步时确定，这里只存最终 URL。
    icon_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        UniqueConstraint("name", name="skills_name_key"),
        Index("skills_attribute_idx", "attribute"),
    )


class TypeMatchupRecord(Base):
    """属性克制矩阵。

    规则本身在 ``app/domain/roco.py`` 的常量里；这张表是它的**物化副本**，
    让检索层可以用一条 SQL 算「这套阵容的弱点分布」，不必在应用层拼 Python 字典。
    """

    __tablename__ = "type_matchup"

    attack_type: Mapped[str] = mapped_column(Text, primary_key=True)
    defend_type: Mapped[str] = mapped_column(Text, primary_key=True)
    multiplier: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    __table_args__ = (Index("type_matchup_defend_idx", "defend_type"),)


class LineupRecord(Base):
    """阵容（玩家投稿）。

    ``wiki_id`` 是 BWIKI 页面 URL 里的 hash，用于去重——同一套阵容可能被
    多个数据源收录（实测两个来源的 151 套里 148 套重合），靠它去重而不是靠标题
    （标题会重名，实测有「平衡攻」出现多次）。
    """

    __tablename__ = "lineups"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    insert_seq: Mapped[int] = _seq_column()
    # BWIKI 的阵容 id（URL 末段 hash）。**去重主键**。
    wiki_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    lineup_type: Mapped[str] = mapped_column(Text, nullable=False, default="pvp")
    author: Mapped[str] = mapped_column(Text, nullable=False, default="")
    intro: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 血脉魔法（整套阵容共用一个）
    blood_magic: Mapped[str] = mapped_column(Text, nullable=False, default="")
    magic_label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 玩家投稿的原始日期字符串（「2026-5-6」这种，不规整，所以存文本不存 timestamp）
    submitted_at: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 游戏内「阵容码」——玩家在编队界面粘贴即可复现这套阵容。
    # **只有 B 站专栏这个源有**（那是游戏导出的格式）；BWIKI 与种子数据为空串。
    # 单独存一列而不是塞进 intro：它是**可执行的数据**，不是说明文字，
    # 前端要给它一个复制按钮。
    import_code: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 六只成员的精灵名，冗余一份用于**快速反查与展示**，避免每次都 JOIN 六行
    member_names: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[dt.datetime] = _now_column()

    __table_args__ = (
        UniqueConstraint("wiki_id", name="lineups_wiki_id_key"),
        CheckConstraint(
            "lineup_type IN ('pvp', 'pve')", name="lineups_type_check"
        ),
        Index("lineups_type_idx", "lineup_type"),
        Index("lineups_title_idx", "title"),
    )


class LineupMemberRecord(Base):
    """阵容成员。

    **「选精灵 → 查阵容」就是查这张表的 ``pet_name`` 索引。**
    这是整个结构化层的核心价值，也是向量检索做不到的事。

    主键是 ``(lineup_id, slot)``：一套阵容最多 6 只，slot 从 1 开始。
    ``lineup_id`` 外键 ON DELETE CASCADE —— 删阵容时成员自动清除，
    与 ``document_chunks`` 用级联是同一个理由：不依赖人工记得同步删。
    """

    __tablename__ = "lineup_members"

    lineup_id: Mapped[str] = mapped_column(
        Text, ForeignKey("lineups.id", ondelete="CASCADE"), primary_key=True
    )
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 阵容原文里的精灵名（可能带形态后缀）
    pet_name: Mapped[str] = mapped_column(Text, nullable=False)
    # 归一化后的基础名（去掉「（平常的样子）」）。
    # **反查走这一列**：用户从图鉴选的是「化蝶」，阵容里写的可能是
    # 「化蝶（平常的样子）」，不归一化就查不到。
    pet_base_name: Mapped[str] = mapped_column(Text, nullable=False)
    bloodline: Mapped[str] = mapped_column(Text, nullable=False, default="")
    nature: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 个体值方向（["生命","魔攻","速度"]）
    talents: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 数值化个体值（{"hp":60,"atk":0,...}），来自 MIT 数据源，可能为空
    iv_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    skills: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    __table_args__ = (
        CheckConstraint("slot >= 1 AND slot <= 6", name="lineup_members_slot_check"),
        # ★ 反查阵容的主索引
        Index("lineup_members_pet_idx", "pet_base_name"),
        Index("lineup_members_pet_exact_idx", "pet_name"),
    )
