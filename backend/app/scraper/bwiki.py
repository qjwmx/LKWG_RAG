"""BWIKI 客户端：抓《洛克王国：世界》的图鉴与阵容。

**反爬是这个模块的核心难点，不是附带功能。**
实测：BWIKI 由腾讯云 EdgeOne 保护，快速请求约 15–20 次后返回
**HTTP 567**（不是 403/429，是个非标准码）。所以：

- **必须限速**：默认 2.5s ± 抖动。官方设计文档也建议 ≥2s。
- **必须带正常 UA + Referer**：裸请求基本立刻被拦。
- **必须支持断点续传**：抓几百条要十几分钟，中途被拦是常态，
  已抓的部分不能丢。
- **567 要单独识别**：它不在 urllib/http 的标准错误码里，
  当成普通 HTTPError 处理会让提示变成"HTTP 错误 567"这种没法行动的信息。

**离线优先**：本项目默认用仓库内的种子数据（``seed_loader``），
爬虫是"想要更新数据时"才跑的可选步骤。抓不到也不影响系统运行。
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_API = "https://wiki.biligame.com/rocom/api.php"
BASE_RAW = "https://wiki.biligame.com/rocom/index.php?action=raw"
BASE_PAGE = "https://wiki.biligame.com/rocom/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}

# 请求间隔（秒）。**不要调小**：这是被 567 拦截的直接原因。
DEFAULT_DELAY = 2.5
# 搜索接口可以快一点（它不是内容页，实测不容易触发拦截）
SEARCH_DELAY = 0.8
# 被拦后的退避基数（秒），每次翻倍
BACKOFF_BASE = 30.0
MAX_RETRIES = 3

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "_scrape_cache"


class AntiBotError(RuntimeError):
    """触发了 EdgeOne 反爬（HTTP 567）。

    单独一个异常类型，因为**处理方式与普通网络错误不同**：
    普通错误重试即可，567 必须退避很久（否则会越试越封）。
    """

    def __init__(self, url: str) -> None:
        super().__init__(
            "触发了 BWIKI 的反爬保护（HTTP 567）。\n"
            f"请求：{url[:100]}\n"
            "建议：等待几分钟后重试，并调大 --delay（默认 2.5 秒）。"
            "已抓取的数据会保留，可用 --resume 继续。"
        )
        self.url = url


# Cookie 持久化：维持会话能显著降低被拦概率
_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))


def _http_get(url: str, timeout: int = 20, referer: str | None = None) -> str:
    """带反爬处理的 GET。567 抛 ``AntiBotError``，其余抛普通异常。"""
    headers = dict(HEADERS)
    headers["Referer"] = referer or BASE_PAGE

    request = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(request, timeout=timeout) as response:
            status = response.getcode()
            if status == 567:
                raise AntiBotError(url)
            body = response.read().decode("utf-8", errors="replace")
            if not body.strip():
                # 空响应通常也是被拦的一种表现（返回了 200 但内容被剥掉）
                raise AntiBotError(url)
            return body
    except urllib.error.HTTPError as exc:
        if exc.code == 567:
            raise AntiBotError(url) from exc
        raise RuntimeError(f"HTTP {exc.code}：{url[:100]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"网络错误：{exc.reason}") from exc


def _get_with_backoff(url: str, referer: str | None = None) -> str:
    """带指数退避的 GET。567 时等待更久再试。"""
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return _http_get(url, referer=referer)
        except AntiBotError as exc:
            last = exc
            wait = BACKOFF_BASE * (2**attempt)
            logger.warning("被反爬拦截，等待 %.0f 秒后重试（第 %d 次）", wait, attempt + 1)
            time.sleep(wait)
        except RuntimeError as exc:
            last = exc
            time.sleep(2.0 * (attempt + 1))
    assert last is not None
    raise last


# --------------------------------------------------------------------------- 阵容


def search_lineup_pages(limit: int | None = None) -> list[str]:
    """搜索所有「精灵阵容/xxx」页面标题。

    用 MediaWiki 的 search API 而不是抓分类页：分类页在 567 之后
    连 HTML 都拿不到，而 search API 返回的是 JSON，体量小、更容易过。
    """
    titles: list[str] = []
    offset = 0
    while True:
        params = {
            "action": "query",
            "list": "search",
            "srsearch": "精灵阵容",
            "format": "json",
            "srlimit": 500,
            "sroffset": offset,
        }
        url = f"{BASE_API}?{urllib.parse.urlencode(params)}"
        payload = json.loads(_get_with_backoff(url))
        results = (payload.get("query") or {}).get("search") or []
        if not results:
            break

        for item in results:
            title = item.get("title") or ""
            # 只保留阵容子页，排除说明页
            if title.startswith("精灵阵容/") and len(title) > len("精灵阵容/"):
                titles.append(title)

        if limit and len(titles) >= limit:
            return titles[:limit]

        total = ((payload.get("query") or {}).get("searchinfo") or {}).get("totalhits", 0)
        offset += len(results)
        if offset >= total:
            break
        time.sleep(max(0.2, SEARCH_DELAY + random.uniform(-0.2, 0.2)))

    return titles[:limit] if limit else titles


def fetch_wikitext(title: str) -> str:
    """取一个页面的 wikitext。

    先试 ``action=parse``（返回 JSON，通常比 raw 更容易过），
    失败再退回 ``action=raw``（返回纯文本）。两条路都试是因为
    不同时间点被拦的路径不一样。
    """
    encoded = urllib.parse.quote(title)
    referer = BASE_PAGE + encoded

    parse_url = f"{BASE_API}?action=parse&page={encoded}&prop=wikitext&format=json"
    try:
        payload = json.loads(_get_with_backoff(parse_url, referer=referer))
        wikitext = ((payload.get("parse") or {}).get("wikitext") or {}).get("*") or ""
        if wikitext:
            return wikitext
    except AntiBotError:
        raise
    except Exception:  # noqa: BLE001 - 退回 raw 再试
        logger.debug("action=parse 失败，回退 action=raw：%s", title)

    return _get_with_backoff(f"{BASE_RAW}&title={encoded}", referer=referer)


_TEMPLATE_RE = re.compile(r"\{\{精灵阵容(.*?)\}\}", re.DOTALL)

# 技能字段里可能混进图片文件名。
#
# 实测（wiki_id d6c6249b01…）：某个成员的技能位是空的，模板里留下了
# `[[文件:技能图标 .png]]` 这类占位，解析出来就是
# 「文件:图标 宠物 属性 .png文件:技能图标 .png」——它不是技能名，
# 但**长得像文本**，会一路进数据库、进前端、进提示词。
# 判定：含「文件:」「.png」「.jpg」或异常长的，一律丢掉。
_SKILL_NOISE_RE = re.compile(r"(文件:|\.png|\.jpg|\.jpeg|\.gif|\[\[|\]\])", re.I)
_MAX_SKILL_NAME = 12


def _clean_skill_name(raw: str) -> str:
    """清洗单个技能名。不是技能名就返回空串（由调用方丢弃）。"""
    name = (raw or "").strip()
    if not name or len(name) > _MAX_SKILL_NAME:
        return ""
    if _SKILL_NOISE_RE.search(name):
        return ""
    return name


def parse_lineup_template(wikitext: str) -> dict | None:
    """解析 ``{{精灵阵容|...}}`` 模板。

    模板字段（实测自 BWIKI）：
        阵容标题 / 阵容编号 / 阵容作者 / 阵容类型 / 阵容介绍 / 阵容血脉魔法
        阵容精灵N / 阵容精灵N血脉 / 阵容精灵N性格 / 阵容精灵N个体值 / 阵容精灵N技能M

    ``N`` 从 1 到 6，``M`` 从 1 到 4。字段可能缺项，缺项按空处理，
    不要因为一个字段缺失就丢掉整条阵容。
    """
    if not wikitext or "精灵阵容" not in wikitext:
        return None

    match = _TEMPLATE_RE.search(wikitext)
    if not match:
        return None

    fields: dict[str, str] = {}
    for raw_line in match.group(1).strip().split("\n"):
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        line = line[1:]
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key.strip()] = value.strip()

    members = []
    for i in range(1, 7):
        pet_name = fields.get(f"阵容精灵{i}", "")
        if not pet_name:
            continue
        skills = [
            cleaned
            for s in range(1, 5)
            if (cleaned := _clean_skill_name(fields.get(f"阵容精灵{i}技能{s}", "")))
        ]
        members.append(
            {
                "slot": i,
                "name": pet_name,
                "bloodline": fields.get(f"阵容精灵{i}血脉", ""),
                "nature": fields.get(f"阵容精灵{i}性格", ""),
                "talents": [
                    t.strip()
                    for t in fields.get(f"阵容精灵{i}个体值", "").replace("，", ",").split(",")
                    if t.strip()
                ],
                "skills": skills,
            }
        )

    if not members:
        return None

    return {
        "wiki_id": fields.get("阵容编号", ""),
        "title": fields.get("阵容标题", "未命名阵容"),
        "lineup_type": (fields.get("阵容类型", "pvp") or "pvp").strip().lower(),
        "author": fields.get("阵容作者", ""),
        "intro": fields.get("阵容介绍", ""),
        "blood_magic": fields.get("阵容血脉魔法", ""),
        "submitted_at": fields.get("阵容上传日期", ""),
        "members": members,
        "source": "bwiki",
    }


# --------------------------------------------------------------------------- 抓取主流程


def _cache_path(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / name


def scrape_lineups(
    limit: int | None = None,
    delay: float = DEFAULT_DELAY,
    resume: bool = True,
    progress=None,
) -> tuple[list[dict], str]:
    """抓取阵容。

    返回 ``(阵容列表, 错误说明)``。**被反爬中断时也返回已抓到的部分**
    （第二个返回值非空），而不是抛异常把已抓的数据一起丢掉。
    """
    cache = _cache_path("lineups.json")
    collected: list[dict] = []
    seen: set[str] = set()

    if resume and cache.exists():
        try:
            existing = json.loads(cache.read_text(encoding="utf-8"))
            collected = existing
            seen = {item.get("wiki_id") or item.get("title", "") for item in existing}
            logger.info("从缓存恢复 %d 套阵容", len(collected))
        except (ValueError, OSError):
            logger.warning("缓存损坏，忽略并重新开始")

    try:
        titles = search_lineup_pages(limit=None)
    except AntiBotError as exc:
        return collected, str(exc)

    # **待抓列表要用页面标题末段的 hash 去比，不能直接拿标题比。**
    # ``search_lineup_pages`` 返回的是「精灵阵容/<hash>」，而缓存里存的是
    # ``wiki_id``（就是那个 hash）。直接 `title not in seen` 永远成立，
    # 于是 --resume 会把 205 个页面**全部重抓一遍**——而它看起来"在工作"，
    # 只是每次都要重跑十几分钟并再被反爬拦一次。
    todo = [t for t in titles if t.rsplit("/", 1)[-1] not in seen]
    if limit:
        todo = todo[:limit]

    logger.info(
        "待抓取 %d 个阵容页面（共发现 %d 个，缓存已有 %d 套）",
        len(todo), len(titles), len(collected),
    )

    error = ""
    for index, title in enumerate(todo):
        try:
            wikitext = fetch_wikitext(title)
            parsed = parse_lineup_template(wikitext)
            if parsed:
                # wiki_id 为空时用页面标题兜底，保证去重键非空
                if not parsed.get("wiki_id"):
                    parsed["wiki_id"] = title.rsplit("/", 1)[-1]
                if parsed["wiki_id"] not in seen:
                    seen.add(parsed["wiki_id"])
                    collected.append(parsed)
                    # **每条即存**：抓几百条要十几分钟，中途被拦是常态，
                    # 攒到最后一起写的话一次中断就全丢了。
                    _save(cache, collected)
            if progress:
                progress(index + 1, len(todo), parsed["title"] if parsed else f"跳过 {title}")
        except AntiBotError as exc:
            error = str(exc)
            logger.warning("被反爬中断，已保存 %d 套：%s", len(collected), exc)
            break
        except Exception as exc:  # noqa: BLE001 - 单条失败不该中断整批
            logger.warning("抓取失败（%s）：%s", title, exc)
            error = f"部分失败：{exc}"

        time.sleep(max(0.5, delay + random.uniform(-0.8, 0.8)))

    _save(cache, collected)
    return collected, error


def _save(path: Path, data) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.exception("写缓存失败：%s", path)
