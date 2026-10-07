"""精灵头像与技能图标的 URL 同步。

**只存 URL，不下载图片**（按需求选择热链）。好处是同步很快、不占磁盘；
代价是依赖 BWIKI 存活。实测 localhost 直接 `<img src>` 能正常加载
（BWIKI 的 patchwiki 没有做 Referer 防盗链）。

两个数据来源，各只需 **1 次请求**：

1. **精灵头像** —— ``精灵图鉴`` 页的 HTML 里每张卡片带 ``JL_<拼音>.png``。
   一次拿到 594 只，归一化后能匹配上本地 371 只里的 **370 只**。
2. **技能图标** —— ``模块:PetDexData/Skills`` 是完整的技能数据表，
   每条带 ``icon_id`` 与 ``name``。一次拿到 781 条，与本地 469 个技能
   **全部匹配**。

图标文件名规则（实测确认）：

- 特性（``category="特性"``）→ ``Feature_<icon_id>.png``
- 其余（攻击/状态）→ ``Skill_<icon_id>.png``

这两条规则是分开的：用错了查不到文件，而且**不报错**——只是图标 404。
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from pathlib import Path

from app.scraper.bwiki import AntiBotError, _get_with_backoff

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "_scrape_cache"
PET_IMAGE_CACHE = "pet_images.json"
SKILL_ICON_CACHE = "skill_icons.json"

# patchwiki 缩略图尺寸。180px 对卡片足够，且比原图小很多。
PET_IMAGE_WIDTH = 180
SKILL_ICON_WIDTH = 56

# 图鉴页里每张精灵卡片的图片：JL_<拼音>.png
_CARD_RE = re.compile(
    r'data-dex-search="[^"]*?".*?dex-card-name.*?title="([^"]+)".*?'
    r'(https://patchwiki\.biligame\.com/images/rocom/[^"\s]+?/(?:\d+px-)?JL_[^"\s]+?\.png)',
    re.DOTALL,
)
# 技能模块里的一条记录：category="X" ... icon_id="N",name="Y"
_SKILL_RE = re.compile(
    r'category="([^"]*)".*?icon_id="(\d+)",name="([^"]+)"',
    re.DOTALL,
)


def _thumb(url: str, width: int) -> str:
    """把 patchwiki 的图片 URL 换成指定宽度的缩略图。

    原 URL 形如 ``.../images/rocom/a/ab/<hash>.png``；
    缩略图形如 ``.../images/rocom/thumb/a/ab/<hash>.png/<W>px-<原名>``。
    已经带 ``/thumb/`` 的就直接替换尺寸段。
    """
    if "/thumb/" in url:
        return re.sub(r"/\d+px-", f"/{width}px-", url)
    # 从原图 URL 构造缩略图：/images/rocom/<a>/<ab>/<hash>.png
    match = re.match(
        r"(https://patchwiki\.biligame\.com/images/rocom)/([0-9a-f])/([0-9a-f]{2})/([^/]+)$",
        url,
    )
    if not match:
        return url
    base, d1, d2, filename = match.groups()
    return f"{base}/thumb/{d1}/{d2}/{filename}/{width}px-{filename}"


def _file_url(filename: str) -> str | None:
    """用 MediaWiki 的 imageinfo 查文件真实 URL。

    只在需要**单张**图标时用（例如按需补抓）；批量同步走模块表，
    不逐文件查询——那是 700+ 次请求。
    """
    encoded = urllib.parse.quote(f"File:{filename}")
    url = (
        "https://wiki.biligame.com/rocom/api.php"
        f"?action=query&titles={encoded}&prop=imageinfo&iiprop=url&format=json"
    )
    payload = json.loads(_get_with_backoff(url))
    for page in ((payload.get("query") or {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("url"):
            return str(info["url"])
    return None


def fetch_pet_images() -> dict[str, str]:
    """从图鉴页抓全部精灵头像。返回 ``{精灵名: 图片URL}``。"""
    encoded = urllib.parse.quote("精灵图鉴")
    url = (
        "https://wiki.biligame.com/rocom/api.php"
        f"?action=parse&page={encoded}&prop=text&format=json"
    )
    payload = json.loads(_get_with_backoff(url))
    html = ((payload.get("parse") or {}).get("text") or {}).get("*") or ""
    if not html:
        raise AntiBotError(url)

    result: dict[str, str] = {}
    for name, image in _CARD_RE.findall(html):
        clean = name.strip()
        if clean and clean not in result:
            result[clean] = _thumb(image, PET_IMAGE_WIDTH)

    logger.info("图鉴页解析到 %d 只精灵头像", len(result))
    return result


def fetch_skill_icons() -> dict[str, str]:
    """从技能数据模块抓全部技能图标 URL。返回 ``{技能名: 图标URL}``。"""
    encoded = urllib.parse.quote("模块:PetDexData/Skills")
    url = (
        "https://wiki.biligame.com/rocom/api.php"
        f"?action=parse&page={encoded}&prop=wikitext&format=json"
    )
    payload = json.loads(_get_with_backoff(url))
    wikitext = ((payload.get("parse") or {}).get("wikitext") or {}).get("*") or ""
    if not wikitext:
        raise AntiBotError(url)

    icons: dict[str, str] = {}
    for category, icon_id, name in _SKILL_RE.findall(wikitext):
        clean = name.strip()
        if not clean or clean in icons:
            continue
        # 特性与普通技能的图标前缀不同，用错会 404 且不报错
        prefix = "Feature" if category == "特性" else "Skill"
        # 直接拼缩略图 URL，省掉逐文件查询
        icons[clean] = _direct_icon_url(prefix, icon_id)

    logger.info("技能模块解析到 %d 个技能图标", len(icons))
    return icons


def _direct_icon_url(prefix: str, icon_id: str) -> str:
    """构造图标的直链。

    用 MediaWiki 的 ``Special:Redirect/file`` 重定向端点，它**不需要知道
    哈希目录**就能拿到文件——这样批量同步不必为每张图查一次 imageinfo
    （781 次请求会被反爬拦掉）。

    浏览器请求时它会 302 到真正的 patchwiki 地址，`<img>` 能正常跟随。
    """
    filename = f"{prefix}_{icon_id}.png"
    return (
        "https://wiki.biligame.com/rocom/Special:Redirect/file/"
        + urllib.parse.quote(filename)
    )


def _save(name: str, data: dict) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_cached(name: str) -> dict:
    path = CACHE_DIR / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logger.warning("缓存损坏：%s", path)
        return {}


def sync_all(force: bool = False) -> dict:
    """同步精灵头像与技能图标。返回统计信息。

    ``force=False`` 时若缓存已存在就跳过——重复跑不该反复打扰 BWIKI。
    """
    stats: dict[str, object] = {}

    pet_cache = load_cached(PET_IMAGE_CACHE)
    if force or not pet_cache:
        try:
            pet_cache = fetch_pet_images()
            _save(PET_IMAGE_CACHE, pet_cache)
        except Exception as exc:  # noqa: BLE001 - 抓不到不该让整个流程失败
            logger.warning("精灵头像同步失败：%s", exc)
    stats["pet_images"] = len(pet_cache)

    skill_cache = load_cached(SKILL_ICON_CACHE)
    if force or not skill_cache:
        try:
            skill_cache = fetch_skill_icons()
            _save(SKILL_ICON_CACHE, skill_cache)
        except Exception as exc:  # noqa: BLE001
            logger.warning("技能图标同步失败：%s", exc)
    stats["skill_icons"] = len(skill_cache)

    return stats
