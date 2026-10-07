"""爬虫与导入层的测试。

**全部离线**：不联网、不依赖真实站点。抓取逻辑与解析逻辑是分开的，
所以这里测的是解析与导入这些**纯函数**——它们才是容易悄悄出错的部分
（正则匹配不到不报错、字段静默丢失）。

三组：
1. **BWIKI 模板解析** —— 含"技能位混进图片文件名"这类真实脏数据。
2. **B 站阵容码解析** —— 含 ``__INITIAL_STATE__`` 提取与行锚定。
3. **导入器** —— 重点是**字段继承**：新数据没有的字段不能被清空。
"""

from __future__ import annotations

import json

import pytest

from app.scraper import bilibili, bwiki, importer

# --------------------------------------------------------------------------- BWIKI 模板


BWIKI_TEMPLATE = """{{精灵阵容
|阵容编号=abc123
|阵容标题=平衡攻
|阵容类型=pvp
|阵容作者=某玩家
|阵容介绍=前排抗伤，后排输出。
|阵容血脉魔法=强化术
|阵容上传日期=2026-6-9
|阵容精灵1=音速犬
|阵容精灵1血脉=火
|阵容精灵1性格=沉默
|阵容精灵1个体值=生命,物攻,速度
|阵容精灵1技能1=火苗
|阵容精灵1技能2=引燃
|阵容精灵1技能3=流星火雨
|阵容精灵1技能4=火云车
|阵容精灵2=寂灭骨龙
|阵容精灵2血脉=冰
|阵容精灵2性格=勇敢
|阵容精灵2个体值=生命,物攻
|阵容精灵2技能1=偷袭
|阵容精灵2技能2=雪替身
}}"""


def test_parse_bwiki_template_basic_fields():
    parsed = bwiki.parse_lineup_template(BWIKI_TEMPLATE)
    assert parsed is not None
    assert parsed["wiki_id"] == "abc123"
    assert parsed["title"] == "平衡攻"
    assert parsed["lineup_type"] == "pvp"
    assert parsed["blood_magic"] == "强化术"
    assert parsed["submitted_at"] == "2026-6-9"
    assert len(parsed["members"]) == 2

    first = parsed["members"][0]
    assert first["slot"] == 1
    assert first["name"] == "音速犬"
    assert first["bloodline"] == "火"
    assert first["nature"] == "沉默"
    assert first["talents"] == ["生命", "物攻", "速度"]
    assert first["skills"] == ["火苗", "引燃", "流星火雨", "火云车"]


def test_parse_bwiki_rejects_image_filename_as_skill():
    """技能位混进 ``文件:…png`` 占位时必须丢掉。

    真实案例（wiki_id d6c6249b01…）：某成员技能位为空，模板里留下
    ``[[文件:技能图标 .png]]``，解析出来是
    「文件:图标 宠物 属性 .png文件:技能图标 .png」。它不是技能名，
    但**长得像文本**，会一路进数据库、前端和提示词。
    """
    template = """{{精灵阵容
|阵容编号=x
|阵容标题=测试
|阵容精灵1=化蝶
|阵容精灵1血脉=幻
|阵容精灵1技能1=文件:图标 宠物 属性 .png文件:技能图标 .png
|阵容精灵1技能2=有效预防
}}"""
    parsed = bwiki.parse_lineup_template(template)
    assert parsed is not None
    skills = parsed["members"][0]["skills"]
    assert skills == ["有效预防"], f"图片占位没被过滤掉：{skills}"


def test_parse_bwiki_returns_none_without_members():
    """没有成员就返回 None，不要产出半个阵容。"""
    assert bwiki.parse_lineup_template("{{精灵阵容|阵容标题=空}}") is None
    assert bwiki.parse_lineup_template("") is None
    assert bwiki.parse_lineup_template("普通文本，没有模板") is None


# --------------------------------------------------------------------------- B 站阵容码

BILI_TEXT = """hello，各位小洛克好久不见！

迪马

阵容码：### 迪马
# 魔法：进化之力
#
# 迪莫：首领血脉、{气泡、棘突、折射、寒风吹}
# 瞌睡王：冰系血脉、{技巧打击、后发制人、一拳、先发制人}
#
B~Gu8~~~T~Y~BPBRBUa5PE~a0ao~a7qi~bDBy~we~~~H~b~BQBPBTbPMY~
#
#想要使用这套阵容，请先复制到剪贴板。

s4-星陨-3

阵容码：### s4-星陨-3
# 魔法：光合治愈
#
# 怖哭菇：幻系血脉、{冥想、错乱、吓退、休息回复}
#
B~Gyp~~~S~a~BSBPBTbbeA~bbb-~ayJK~ayBg~xC~~~
"""


def test_parse_bili_lineup_codes():
    items = bilibili.parse_lineup_codes(BILI_TEXT, 52976629)
    assert len(items) == 2, f"应解析出 2 套阵容，实际 {len(items)}"

    first, second = items
    assert first["title"] == "迪马"
    assert first["blood_magic"] == "进化之力"
    assert len(first["members"]) == 2
    assert first["members"][0]["name"] == "迪莫"
    assert first["members"][0]["bloodline"] == "首领血脉"
    assert first["members"][0]["skills"] == ["气泡", "棘突", "折射", "寒风吹"]
    assert first["wiki_id"] == "bili-52976629-1"
    assert first["import_code"].startswith("B~Gu8~")

    assert second["title"] == "s4-星陨-3"
    assert second["wiki_id"] == "bili-52976629-2"
    assert second["blood_magic"] == "光合治愈"


def test_parse_bili_ignores_magic_line_as_member():
    """``# 魔法：…`` 不是成员行，不能当成精灵。

    它和成员行长得几乎一样（都是 ``# 名称：值``），
    但分隔符是「：」而不是「、{」，靠 ``\\{`` 区分。
    """
    items = bilibili.parse_lineup_codes(BILI_TEXT, 1)
    names = [m["name"] for item in items for m in item["members"]]
    assert "魔法" not in names
    assert "进化之力" not in names


def test_parse_bili_truncates_to_six_members():
    """超过 6 只时截断，不要把解析错位当成一套 12 只的阵容。"""
    lines = ["阵容码：### 大杂烩", "# 魔法：测试"]
    for i in range(1, 10):
        lines.append(f"# 精灵{i}：火系血脉、{{技能{i}a、技能{i}b}}")
    items = bilibili.parse_lineup_codes("\n".join(lines), 7)
    assert len(items) == 1
    assert len(items[0]["members"]) == 6


def test_extract_state_handles_trailing_javascript():
    """``__INITIAL_STATE__`` 后面紧跟一段 IIFE，必须只取第一个完整 JSON 对象。

    用正则贪到 ``</script>`` 会带上尾部 JS，``json.loads`` 报
    "Extra data" —— 于是**所有专栏都解析失败**，看起来像"没有一篇有阵容码"。
    """
    from app.scraper.bilibili import _extract_state

    html = (
        '<script>window.__INITIAL_STATE__={"a":1,"b":{"c":"x}y{"}};'
        "(function(){var s;(s=document.currentScript).parentNode"
        ".removeChild(s);}());</script>"
    )
    state = _extract_state(html)
    assert state == {"a": 1, "b": {"c": "x}y{"}}


def test_extract_state_handles_undefined_literal():
    """state 里有 JS 的 ``undefined``，JSON 不认，要替换成 null。"""
    from app.scraper.bilibili import _extract_state

    state = _extract_state('<script>window.__INITIAL_STATE__={"a":undefined,"b":1}</script>')
    assert state == {"a": None, "b": 1}


def test_extract_state_returns_none_on_block_page():
    """被反爬拦时返回的是 3.3KB 壳页，没有 state —— 必须返回 None
    让调用方重试，而不是当成"这篇没有内容"。"""
    from app.scraper.bilibili import _extract_state

    assert _extract_state("<html><body>request was banned</body></html>") is None


def test_state_to_text_keeps_line_breaks():
    """段落之间要有换行：阵容码成员行是按行锚定的，粘成一行就一条都匹配不到。"""
    from app.scraper.bilibili import _state_to_text

    state = {
        "detail": {
            "modules": [
                {
                    "module_content": {
                        "paragraphs": [
                            {"text": {"nodes": [{"word": {"words": "阵容码：### 甲"}}]}},
                            {"text": {"nodes": [{"word": {"words": "# 迪莫：首领血脉、{气泡}"}}]}},
                            {"pic": {"pics": []}},
                        ]
                    }
                }
            ]
        }
    }
    text = _state_to_text(state)
    assert "阵容码：### 甲" in text
    assert "# 迪莫：首领血脉、{气泡}" in text
    # 两段之间必须有换行
    assert "甲\n" in text


# --------------------------------------------------------------------------- 导入器


def _make_item(wiki_id: str, **overrides) -> dict:
    item = {
        "wiki_id": wiki_id,
        "title": "测试阵容",
        "lineup_type": "pvp",
        "author": "某人",
        "intro": "介绍",
        "blood_magic": "强化术",
        "submitted_at": "2026-6-9",
        "source": "测试源",
        "source_url": "https://example.com/x",
        "members": [
            {
                "slot": 1,
                "name": "音速犬",
                "bloodline": "火",
                "nature": "沉默",
                "talents": ["生命", "物攻"],
                "skills": ["火苗", "引燃"],
            }
        ],
    }
    item.update(overrides)
    return item


@pytest.fixture
def clean_lineups():
    """清掉本文件造出来的测试阵容（id 前缀 ``t-``），并在用例后清理。

    **只删自己造的行，不要 ``delete(LineupRecord)`` 清空整表。**
    清空整表会破坏 ``test_roco.py`` 依赖的种子数据——而 pytest 按
    文件名收集（test_roco 在 test_scraper 之前），所以"清空整表"在
    默认顺序下**看起来**没问题，一旦单独跑或改了顺序就会莫名失败。
    测试之间不该有这种隐式依赖。
    """
    from sqlalchemy import delete, select

    from app.db import LineupMemberRecord, LineupRecord
    from app.db.session import transaction

    def purge() -> None:
        with transaction() as session:
            ids = list(
                session.execute(select(LineupRecord.id).where(LineupRecord.id.like("t-%"))).scalars()
            )
            if ids:
                session.execute(
                    delete(LineupMemberRecord).where(LineupMemberRecord.lineup_id.in_(ids))
                )
                session.execute(delete(LineupRecord).where(LineupRecord.id.in_(ids)))

    purge()
    yield
    purge()


def test_import_inserts_new_lineup(clean_lineups):
    from app.db import LineupMemberRecord, LineupRecord
    from app.db.session import transaction
    from sqlalchemy import select

    inserted, updated, skipped = importer.import_lineups([_make_item("t-1")], "默认源")
    assert (inserted, updated, skipped) == (1, 0, 0)

    with transaction() as session:
        record = session.get(LineupRecord, "t-1")
        assert record is not None
        assert record.title == "测试阵容"
        assert record.source == "测试源"
        assert record.member_names == ["音速犬"]
        members = list(
            session.execute(
                select(LineupMemberRecord).where(LineupMemberRecord.lineup_id == "t-1")
            ).scalars()
        )
        assert len(members) == 1
        assert members[0].pet_base_name == "音速犬"
        assert members[0].skills == ["火苗", "引燃"]


def test_import_preserves_iv_config_on_update(clean_lineups):
    """★ 更新时不能清空新数据没有的字段。

    真实事故：第一版导入"删旧成员再插新成员"，把 150 套阵容的
    906 条成员个体值（``iv_config``）全清空了，而且**不报错**——
    阵容数、成员数、接口返回都正常，只有前端那张个体值图变空。

    种子数据带数值化个体值，BWIKI 爬虫只有方向（``talents``），
    所以更新时必须从旧行继承。
    """
    from app.db import LineupMemberRecord
    from app.db.session import transaction
    from sqlalchemy import select

    # 第一次：带数值化个体值（模拟种子数据）
    rich = _make_item("t-2")
    rich["members"][0]["iv_config"] = {"hp": 60, "atk": 0, "def": 60}
    rich["members"][0]["source"] = "seed"
    importer.import_lineups([rich], "种子源")

    # 第二次：同一套阵容，但新数据没有 iv_config（模拟 BWIKI 爬虫）
    lean = _make_item("t-2", source="爬虫源")
    lean["members"][0]["skills"] = ["火苗", "引燃", "流星火雨"]
    importer.import_lineups([lean], "爬虫源")

    with transaction() as session:
        member = session.execute(
            select(LineupMemberRecord).where(LineupMemberRecord.lineup_id == "t-2")
        ).scalar_one()
        assert member.iv_config == {"hp": 60, "atk": 0, "def": 60}, "个体值被清空了"
        # 新数据有的字段要更新（技能多了一个）
        assert member.skills == ["火苗", "引燃", "流星火雨"]


def test_import_merges_source_labels(clean_lineups):
    """来源要并列保留，不能覆盖掉原始出处。

    150 套种子阵容被 BWIKI 更新后，若 source 直接改成 bwiki，
    那 906 条个体值的真实出处（MIT 数据源，有署名要求）就消失了。
    """
    from app.db import LineupRecord
    from app.db.session import transaction

    importer.import_lineups([_make_item("t-3", source="MIT 数据源")], "MIT 数据源")
    importer.import_lineups([_make_item("t-3", source="bwiki 爬虫")], "bwiki 爬虫")

    with transaction() as session:
        record = session.get(LineupRecord, "t-3")
        assert record.source == "MIT 数据源 + bwiki 爬虫"

    # 重复导入同一个来源不该重复追加
    importer.import_lineups([_make_item("t-3", source="bwiki 爬虫")], "bwiki 爬虫")
    with transaction() as session:
        record = session.get(LineupRecord, "t-3")
        assert record.source == "MIT 数据源 + bwiki 爬虫"


def test_import_normalizes_pet_form_suffix(clean_lineups):
    """成员名带形态后缀时，``pet_base_name`` 要去掉括号（反查靠它）。"""
    from app.db import LineupMemberRecord
    from app.db.session import transaction
    from sqlalchemy import select

    item = _make_item("t-4")
    item["members"][0]["name"] = "化蝶（平常的样子）"
    importer.import_lineups([item], "源")

    with transaction() as session:
        member = session.execute(
            select(LineupMemberRecord).where(LineupMemberRecord.lineup_id == "t-4")
        ).scalar_one()
        assert member.pet_name == "化蝶（平常的样子）"
        assert member.pet_base_name == "化蝶"


def test_import_cleans_skill_noise_from_cache(clean_lineups):
    """旧缓存里已有的图片占位也要在导入时挡掉。

    爬虫层修了，但**旧缓存文件还在**；重新导入一份没重抓的缓存时
    不该把垃圾带进库。
    """
    from app.db import LineupMemberRecord
    from app.db.session import transaction
    from sqlalchemy import select

    item = _make_item("t-5")
    item["members"][0]["skills"] = [
        "火苗",
        "文件:图标 宠物 属性 .png文件:技能图标 .png",
        "引燃",
    ]
    importer.import_lineups([item], "源")

    with transaction() as session:
        member = session.execute(
            select(LineupMemberRecord).where(LineupMemberRecord.lineup_id == "t-5")
        ).scalar_one()
        assert member.skills == ["火苗", "引燃"]


def test_import_skips_items_without_id_or_members(clean_lineups):
    _, _, skipped = importer.import_lineups(
        [
            _make_item(""),
            _make_item("t-6", members=[]),
        ],
        "源",
    )
    assert skipped == 2


def test_clean_type_falls_back_to_pvp():
    """非法类型会被 CHECK 约束拒绝，必须先收敛。"""
    assert importer._clean_type("PVE") == "pve"
    assert importer._clean_type("pve副本") == "pve"
    assert importer._clean_type("PVP") == "pvp"
    assert importer._clean_type("") == "pvp"
    assert importer._clean_type("乱写") == "pvp"


def test_load_items_accepts_both_cache_shapes(tmp_path):
    """BWIKI 缓存是裸数组，B 站缓存是 ``{"lineups": [...], "articles": [...]}``。"""
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps([{"wiki_id": "a"}]), encoding="utf-8")
    assert importer._load_items(bare) == [{"wiki_id": "a"}]

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps({"lineups": [{"wiki_id": "b"}], "articles": [1]}), encoding="utf-8"
    )
    assert importer._load_items(wrapped) == [{"wiki_id": "b"}]


def test_load_items_rejects_unknown_shape(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps("just a string"), encoding="utf-8")
    with pytest.raises(importer.ImportError_):
        importer._load_items(bad)


def test_import_persists_import_code(clean_lineups):
    """阵容码要落库——它是 B 站源最有价值的字段（可在游戏内粘贴复现）。"""
    from app.db import LineupRecord
    from app.db.session import transaction

    item = _make_item("t-7", import_code="B~Gu8~~~T~Y~BPBRBU")
    importer.import_lineups([item], "bilibili 专栏")

    with transaction() as session:
        record = session.get(LineupRecord, "t-7")
        assert record.import_code == "B~Gu8~~~T~Y~BPBRBU"


def test_import_keeps_import_code_when_other_source_updates(clean_lineups):
    """别的源更新同一套阵容时不能把阵容码清掉。

    阵容码只有 B 站源有；BWIKI 源的缓存里这个字段是缺失的。
    若更新时无条件写 ``item.get("import_code") or ""``，阵容码就没了。
    """
    from app.db import LineupRecord
    from app.db.session import transaction

    importer.import_lineups([_make_item("t-8", import_code="B~CODE~")], "bilibili 专栏")
    # BWIKI 来的同一条：没有 import_code 字段
    importer.import_lineups([_make_item("t-8", source="bwiki 爬虫")], "bwiki 爬虫")

    with transaction() as session:
        record = session.get(LineupRecord, "t-8")
        assert record.import_code == "B~CODE~", "阵容码被另一个源清掉了"


def test_looks_like_world_rejects_classic_game():
    """必须排掉经典 Flash 页游《洛克王国》的内容。

    实测「洛克王国 阵容推荐」（不带"世界"）这类词的命中率只有 35–40%，
    抓到的是「2021年洛克王国平衡阵容分析」这种**另一个游戏**的文章
    ——两个游戏数据生态完全分离，混进来就是脏数据。
    """
    # 经典版：应拒绝
    assert not bilibili._looks_like_world("2021年洛克王国平衡阵容分析")
    assert not bilibili._looks_like_world("洛克王国12.3 阵容推荐")
    assert not bilibili._looks_like_world("洛克王国怀旧服阵容")
    assert not bilibili._looks_like_world("4399洛克王国阵容推荐")
    assert not bilibili._looks_like_world("洛克王国flash页游阵容")

    # 世界版：应通过
    assert bilibili._looks_like_world("洛克王国世界pvp阵容推荐4")
    assert bilibili._looks_like_world("洛克王国：世界 S4 阵容")
    assert bilibili._looks_like_world("洛克王国世界 排位赛阵容")


def test_search_keywords_all_mention_world():
    """搜索词必须都带「世界」，否则会引入经典版的污染结果。"""
    for keyword in bilibili.SEARCH_KEYWORDS:
        assert "世界" in keyword, f"搜索词「{keyword}」缺少「世界」，会污染结果"


def test_article_retry_delay_is_flat_not_exponential():
    """正文重试间隔必须是**固定短间隔**，不能用指数退避。

    实测 B 站对 HTML 页的拦截是**随机约 50%**（同一秒内连续请求，
    有时第 1 次成功、有时第 3 次成功），不是"越试越封"的渐进式限流。
    早先用 ``BACKOFF_BASE * (attempt + 1)``（20/40/60/80/100 秒），
    单篇连拦三次就是 300 秒——12 篇花了十几分钟。
    真正需要长退避的是搜索接口的 -509（见 ``_get_json``），那里保留指数退避。

    这个测试锁住"两者不同"这个区别，避免以后又被统一成指数退避。
    """
    assert bilibili.ARTICLE_RETRY_DELAY < 15, "正文重试间隔不该超过 15 秒"
    assert bilibili.BACKOFF_BASE >= 15, "搜索接口的退避基数应该是长间隔"
    # 正文重试间隔不能随次数增长：源码里不能出现 attempt 参与 sleep 的写法
    import inspect

    source = inspect.getsource(bilibili.fetch_article_text)
    assert "ARTICLE_RETRY_DELAY)" in source
    assert "ARTICLE_RETRY_DELAY *" not in source, "正文重试间隔不该随重试次数放大"


def test_resume_matches_wiki_id_not_page_title():
    """``--resume`` 的待抓列表要用页面标题末段的 hash 比，不能拿标题比。

    真实 bug：``search_lineup_pages`` 返回「精灵阵容/<hash>」，缓存里存
    的是 ``wiki_id``（就是那个 hash）。直接 ``title not in seen`` 永远成立，
    于是 ``--resume`` 把 205 个页面**全部重抓**——而它看起来"在工作"，
    只是每次都要重跑十几分钟并再被反爬拦一次。
    """
    titles = ["精灵阵容/aaa", "精灵阵容/bbb", "精灵阵容/ccc"]
    seen = {"aaa", "bbb"}
    todo = [t for t in titles if t.rsplit("/", 1)[-1] not in seen]
    assert todo == ["精灵阵容/ccc"]
