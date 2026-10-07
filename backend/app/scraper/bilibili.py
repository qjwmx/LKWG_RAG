"""B 站专栏阵容爬虫：抓「阵容码」格式的玩家配队。

为什么需要这个源
----------------
BWIKI 的阵容页最新投稿停在 2026-06，而当前是 S4 赛季（2026-09 开启）。
B 站专栏里有大量**当赛季**的配队分享，而且用的是游戏内置的
**「阵容码」导出格式**，字段结构固定、可机器解析：

    ### 迪马
    # 魔法：进化之力
    #
    # 迪莫：首领血脉、{气泡、棘突、折射、寒风吹}
    # 瞌睡王：冰系血脉、{技巧打击、后发制人、一拳、先发制人}
    ...
    B~Gu8~~~T~Y~BPBRBUa5PE~...

所以这个源的价值不是"又一个网页"，而是**唯一能拿到 S4 赛季阵容**的源。

与 BWIKI 的差别（导入时必须区分）
--------------------------------
- ``wiki_id`` 用 ``bili-<aid>-<序号>``：B 站没有稳定的阵容 id，
  但同一篇文章内序号是确定的，所以这个键可复现、可用于去重。
- ``source`` 标成 ``bilibili 专栏``，``source_url`` 指向 ``/read/cv<aid>/``。
- 没有 ``author`` 之外的元信息（性格、个体值），**不编造**：
  阵容码里没有的字段一律留空，而不是猜一个默认值填进去。

反爬：不是 -403 而是 code=-509 / -352，以及文章接口会直接封 IP
--------------------------------------------------------------
B 站的限流**返回 HTTP 200**，限流信息藏在 JSON 的 ``code`` 里：
- ``-509`` 请求过于频繁
- ``-352`` 风控校验失败
- ``-412`` ``request was banned`` —— 文章接口连续请求后会把 IP 拉黑一段时间

这两者都**不能当"没有结果"处理**——早期版本用 ``/wbi/search/type``
（需要 wbi 签名），未签名时它返回 ``code=0`` + 空 ``result``，
看起来像"这个词没内容"，实际是接口用错了。所以：

- 搜索走 ``/x/web-interface/search/type``（**不加 wbi 前缀**）
- 每个请求之间强制 sleep，遇到 -509/-352 指数退避重试
- 失败的单条跳过而不是中断整批

**正文不从 ``/x/article/view`` 取，而是从专栏 HTML 页里挖 ``__INITIAL_STATE__``。**
实测那个 API 抓十几篇就会被 ``-412`` 拉黑，而 HTML 页在同样条件下
**约一半请求能成功**（返回约 98KB；被拦时返回 3.3KB 的壳页）。
所以正文抓取要"重试到拿到带 state 的页面为止"，而不是一次定生死。
页面的 state 里正文在 ``detail.modules[*].module_content.paragraphs``，
每段的文字在 ``text.nodes[*].word.words``。
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

BASE_SEARCH = "https://api.bilibili.com/x/web-interface/search/type"
HOME = "https://www.bilibili.com/"

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "_scrape_cache"
CACHE_NAME = "lineups_bilibili.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 搜索词。**多组是必要的**：B 站的搜索按相关性排，
# 单用「阵容」会大量返回 PVE 抓捕队和版本公告。
#
# **每个词都必须带「世界」**。实测去掉它（例如只搜「洛克王国 阵容推荐」）
# 会把**经典 Flash 页游《洛克王国》**的结果混进来——命中率只有 35–40%，
# 而带「世界」的词是 70–95%。两个游戏的数据生态完全分离
# （见 ``app/domain/roco.py`` 开头），混进来的是另一个游戏的配队，
# 抓 82 篇一篇阵容码都没多出来。
SEARCH_KEYWORDS = [
    "洛克王国世界 阵容码",
    "洛克王国世界 阵容",
    "洛克王国世界 pvp阵容",
    "洛克王国世界 pvp",
    "洛克王国世界 配队",
    "洛克王国世界 s4 阵容",
    "洛克王国世界 s4",
    "洛克王国世界 上分",
    "洛克王国世界 大师",
    "洛克王国世界 排位",
    "洛克王国世界 队伍",
    "洛克王国世界 对战",
    "洛克王国世界 赛季",
]

# 请求间隔（秒）。B 站限流阈值比 BWIKI 低很多，实测 1s 连发几次就 -509。
SEARCH_DELAY = 2.0
VIEW_DELAY = 3.0
# 被限流后的退避基数（秒）
BACKOFF_BASE = 20.0
MAX_RETRIES = 4
# 正文页重试次数。实测 HTML 页约一半请求被拦（返回 3.3KB 壳页），
# 所以必须重试；5 次全被拦的概率约 3%，可以接受。
ARTICLE_RETRIES = 5
# 正文页被拦后的重试间隔（秒）。
#
# **这里刻意不用指数退避**：实测拦截是**随机约 50%**（同一秒内连续请求，
# 有时第 1 次成功、有时第 3 次成功），不是"越试越封"的渐进式限流。
# 早先用 ``BACKOFF_BASE * (attempt + 1)``（20/40/60/80/100 秒），
# 单篇被拦一次就要多等 20 秒、连拦三次就是 300 秒——实测 12 篇花了
# 十几分钟。改成固定短间隔后同样的成功率下快一个数量级。
# 真正需要长退避的是搜索接口的 ``-509``（见 ``_get_json``），那里保留指数退避。
ARTICLE_RETRY_DELAY = 6.0
SEARCH_PAGES = 3

# 只收标题里带这些词的（排掉版本公告、单精灵解析等）
_TITLE_HINTS = ("阵容", "配队", "队伍", "队")

# 经典 Flash 页游《洛克王国》的标题特征。**必须排除**：
# 两个游戏数据生态完全分离，经典版的精灵/技能名与本项目图鉴对不上，
# 抓进来就是脏数据（实测这类文章一篇「阵容码」都没有）。
_CLASSIC_TITLE_RE = re.compile(
    r"(20(?:1|2)\d\s*年|flash|页游|怀旧|4399|洛克王国\s*\d+\.\d+)", re.I
)


def _looks_like_world(title: str) -> bool:
    """判断这篇是不是《洛克王国：世界》的内容。

    **只做排除，不做正向要求**：标题里出现经典版特征（年份、flash、
    页游、4399、版本号）就丢掉。不要求标题必须带「世界」，因为有些
    世界版的配队文标题也不写它——要求带「世界」会漏抓。

    「世界」这个正向信号由 ``SEARCH_KEYWORDS`` 保证（每个词都带它，
    实测命中率从 35–40% 提到 70–95%），标题层只兜底。

    保守取向：**宁可漏抓也不要抓错游戏**。抓错的代价是把另一个游戏的
    精灵和技能混进库，会让「选精灵反查阵容」返回完全无关的结果，
    而且很难发现是数据源的问题。
    """
    return not _CLASSIC_TITLE_RE.search(title)


# 阵容码里的成员行：`# 迪莫：首领血脉、{气泡、棘突、折射、寒风吹}`
# 用 ``^``/``$`` 锚定 + ``(?!#)`` 排除 `# 魔法：` 这类非成员行。
_MEMBER_RE = re.compile(r"^#\s*(?!#)([^#：:]+?)：([^、{}]+)、\{([^}]+)\}\s*$", re.M)
_TITLE_RE = re.compile(r"阵容码[：:]\s*###\s*(.+)")
_MAGIC_RE = re.compile(r"^#\s*魔法[：:]\s*(.+)$", re.M)
# 阵容码本体：一行由 ~ 和字母数字组成的紧凑串
_CODE_RE = re.compile(r"^[A-Za-z0-9~_\-]{40,}$", re.M)


class BiliError(RuntimeError):
    """B 站接口返回了非 0 的业务码。"""


def _new_session() -> requests.Session:
    """建会话并取一次首页。

    必须先访问首页：它会下发 ``buvid3`` cookie，没有这个 cookie
    搜索接口直接返回空结果（同样不报错）。
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Referer": HOME})
    try:
        session.get(HOME, timeout=20)
    except requests.RequestException as exc:  # noqa: BLE001 - 拿不到 cookie 也要继续试
        logger.warning("访问 B 站首页失败（可能拿不到 buvid3）：%s", exc)
    return session


def _get_json(
    session: requests.Session, url: str, params: dict, referer: str, delay: float
) -> dict:
    """带限流退避的 GET。

    -509/-352 要退避很久再试；其余业务码直接抛。
    """
    last_error = ""
    for attempt in range(MAX_RETRIES):
        time.sleep(max(0.4, delay + random.uniform(-0.3, 0.6)))
        try:
            response = session.get(url, params=params, headers={"Referer": referer}, timeout=25)
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.warning("B 站请求失败（第 %d 次）：%s", attempt + 1, last_error)
            continue

        code = payload.get("code")
        if code == 0:
            return payload
        if code in (-509, -352):
            wait = BACKOFF_BASE * (2**attempt)
            last_error = f"code={code}（限流/风控）"
            logger.warning("B 站限流，等待 %.0f 秒后重试（第 %d 次）", wait, attempt + 1)
            time.sleep(wait)
            continue
        raise BiliError(f"B 站接口 code={code} msg={payload.get('message')!r}")

    raise BiliError(f"B 站请求重试 {MAX_RETRIES} 次仍失败：{last_error}")


def _extract_state(html: str) -> dict | None:
    """从专栏页里挖出 ``window.__INITIAL_STATE__`` 的 JSON。

    两个坑：
    1. 该脚本后面紧跟一段 IIFE（``;(function(){...}())``），
       所以**不能**用正则一路贪到 ``</script>`` —— 会带上尾部的 JS，
       ``json.loads`` 报 "Extra data"。用花括号深度扫描取第一个完整对象。
    2. state 里有 JS 的 ``undefined`` 字面量，JSON 不认，先替换成 ``null``。
       字符串内的 ``undefined`` 会一起被替换，但正文里出现这个词的概率极低，
       且替换后仍是合法 JSON —— 可接受的取舍。
    """
    marker = "__INITIAL_STATE__"
    index = html.find(marker)
    if index < 0:
        return None
    body = html[html.find("=", index) + 1 :]

    depth = 0
    in_string = False
    escaped = False
    end = None
    for position, char in enumerate(body):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = position + 1
                break

    if end is None:
        return None
    try:
        return json.loads(re.sub(r"\bundefined\b", "null", body[:end]))
    except ValueError:
        return None


def _state_to_text(state: dict) -> str:
    """把 state 里的正文段落拼成纯文本（每段一行）。

    正文位置：``detail.modules[*].module_content.paragraphs``。
    文字在 ``paragraph.text.nodes[*].word.words``。**图片段没有 text**，
    跳过即可——我们要的是阵容码，它在文字段里。
    """
    detail = state.get("detail") or {}
    if not isinstance(detail, dict):
        return ""

    lines: list[str] = []
    for module in detail.get("modules") or []:
        content = (module or {}).get("module_content") or {}
        for paragraph in content.get("paragraphs") or []:
            text = paragraph.get("text")
            if not isinstance(text, dict):
                continue
            for node in text.get("nodes") or []:
                word = (node or {}).get("word") or {}
                piece = word.get("words")
                if piece:
                    lines.append(piece)
            # 段落之间补一个换行：阵容码的成员行是按行锚定解析的，
            # 粘成一行会导致一条都匹配不到。
            lines.append("")
    return "\n".join(lines)


def fetch_article_text(session: requests.Session, aid: int, delay: float) -> tuple[str, dict]:
    """取一篇专栏的正文与元信息。

    返回 ``(正文, 元信息)``。**重试到拿到带 state 的页面为止**：
    被拦时页面只有约 3.3KB 的壳，没有 state；正常时约 98KB。
    这是实测最可靠的路子——正文 API 会直接 -412 封 IP，
    而 HTML 页只是间歇性拦截。
    """
    url = f"https://www.bilibili.com/read/cv{aid}/"
    last = ""

    for attempt in range(ARTICLE_RETRIES):
        time.sleep(max(0.5, delay + random.uniform(-0.3, 0.6)))
        try:
            response = session.get(
                url,
                headers={"Referer": HOME, "Accept": "text/html,application/xhtml+xml"},
                timeout=25,
            )
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            logger.warning("专栏 cv%d 请求失败（第 %d 次）：%s", aid, attempt + 1, last)
            continue

        state = _extract_state(response.text)
        if state is None:
            last = f"HTTP {response.status_code}，页面无 __INITIAL_STATE__（{len(response.text)} 字节）"
            logger.warning("专栏 cv%d 被拦（第 %d 次）：%s", aid, attempt + 1, last)
            # 固定短间隔重试，**不要指数退避**：拦截是随机的，不是越试越封。
            # 详见 ARTICLE_RETRY_DELAY 的说明。
            time.sleep(ARTICLE_RETRY_DELAY)
            continue

        text = _state_to_text(state)
        if not text.strip():
            last = "state 里没有正文段落"
            logger.warning("专栏 cv%d %s", aid, last)
            continue

        detail = state.get("detail") or {}
        basic = detail.get("basic") or {}
        # 作者与发布时间**不在 basic 里**：basic 只有 title/uid，且没有
        # publish_time（实测为 None）。真实位置是 ``modules[*].module_author``
        # （name / mid / pub_ts）。早先按 basic.author 取，结果 author 恒为空——
        # 不报错，只是署名丢了。
        author = ""
        publish_time = 0
        for module in detail.get("modules") or []:
            module_author = (module or {}).get("module_author") or {}
            if not author and module_author.get("name"):
                author = str(module_author["name"]).strip()
            if not publish_time and module_author.get("pub_ts"):
                publish_time = int(module_author["pub_ts"])
            if author and publish_time:
                break

        meta = {
            "title": (basic.get("title") or "").strip(),
            "author": author,
            "publish_time": publish_time or basic.get("publish_time") or 0,
        }
        return text, meta

    raise BiliError(f"专栏 cv{aid} 重试 {ARTICLE_RETRIES} 次仍拿不到正文：{last}")


def _to_date(publish_time: int | None) -> str:
    """把发布秒级时间戳转成 ``YYYY-M-D``，与 BWIKI 的日期写法保持一致。

    不补零是刻意的：``lineups.submitted_at`` 是**文本**列，BWIKI 侧
    存的是「2026-5-7」这种不规整格式，两边写法一致才能按字符串排序比较。
    """
    if not publish_time:
        return ""
    try:
        moment = datetime.fromtimestamp(int(publish_time), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return f"{moment.year}-{moment.month}-{moment.day}"


def parse_lineup_codes(text: str, aid: int, fallback_title: str = "") -> list[dict]:
    """从专栏正文里解析出全部「阵容码」阵容。

    一篇专栏常含多套阵容，所以返回列表。解析规则：
    - ``阵容码：### <标题>`` 开启一套
    - ``# 魔法：<血脉魔法>`` 是整套共用的魔法
    - ``# <精灵>：<血脉>、{技能...}`` 是成员（1~6 个）
    - 结尾那串紧凑字符是游戏内导入码，**原样保留**（``import_code``），
      用户可以直接复制进游戏，这是这个数据源最有价值的部分

    解析不到成员就丢弃该段——宁缺毋滥，半个阵容比没有更糟。
    """
    lineups: list[dict] = []
    lines = text.splitlines()

    current: dict | None = None
    member_index = 0

    def flush() -> None:
        nonlocal current, member_index
        if current and current["members"]:
            member_index += 1
            current["wiki_id"] = f"bili-{aid}-{member_index}"
            lineups.append(current)
        current = None

    for line in lines:
        title_match = _TITLE_RE.search(line)
        if title_match:
            flush()
            current = {
                "title": title_match.group(1).strip() or fallback_title,
                "blood_magic": "",
                "members": [],
                "import_code": "",
                "source": "bilibili",
                "article_id": aid,
            }
            continue

        if current is None:
            continue

        magic_match = _MAGIC_RE.match(line.strip())
        if magic_match:
            current["blood_magic"] = magic_match.group(1).strip()
            continue

        member_match = _MEMBER_RE.match(line)
        if member_match:
            skills = [s.strip() for s in member_match.group(3).split("、") if s.strip()]
            if not skills:
                continue
            current["members"].append(
                {
                    "slot": len(current["members"]) + 1,
                    "name": member_match.group(1).strip(),
                    "bloodline": member_match.group(2).strip(),
                    "nature": "",
                    "talents": [],
                    "skills": skills,
                }
            )
            continue

        if not current["import_code"] and _CODE_RE.match(line.strip()):
            current["import_code"] = line.strip()

    flush()

    # 超过 6 只的截断（游戏上限 6）：解析错位时不要把 12 只当成一套阵容
    for item in lineups:
        item["members"] = item["members"][:6]
    return [item for item in lineups if item["members"]]


def search_articles(session: requests.Session, pages: int = SEARCH_PAGES) -> list[dict]:
    """按多组关键词搜索专栏，按 aid 去重。

    **按发布时间倒序返回**：候选池有 400 篇左右，而正文抓取每篇要
    几秒到几十秒（还要重试反爬），全量抓完要很久。配队的时效性极强
    ——当赛季的阵容才有参考价值，2026-04 的老阵容可能已经过时。
    所以先抓最新的，配合 ``--limit`` 能在有限时间里拿到最有用的部分。
    """
    found: dict[int, dict] = {}
    for keyword in SEARCH_KEYWORDS:
        for page in range(1, pages + 1):
            try:
                payload = _get_json(
                    session,
                    BASE_SEARCH,
                    {"search_type": "article", "keyword": keyword, "page": page},
                    referer="https://search.bilibili.com/",
                    delay=SEARCH_DELAY,
                )
            except BiliError as exc:
                logger.warning("搜索失败（%s p%d）：%s", keyword, page, exc)
                continue

            results = (payload.get("data") or {}).get("result") or []
            if not results:
                break
            for item in results:
                aid = item.get("id")
                title = re.sub(r"<[^>]+>", "", item.get("title") or "").strip()
                if not aid or aid in found:
                    continue
                # 标题过滤：版本公告、单精灵解析等没有配队内容
                if not any(hint in title for hint in _TITLE_HINTS):
                    continue
                # 排掉经典 Flash 页游《洛克王国》的内容（另一个游戏）
                if not _looks_like_world(title):
                    logger.debug("跳过疑似经典版内容：%s", title[:40])
                    continue
                found[int(aid)] = {
                    "id": int(aid),
                    "title": title,
                    "pubdate": int(item.get("pubdate") or 0),
                }
            logger.info("搜索「%s」第 %d 页：累计 %d 篇", keyword, page, len(found))
    return sorted(found.values(), key=lambda a: a["pubdate"], reverse=True)


def _cache_path() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / CACHE_NAME


def _load_cache() -> tuple[list[dict], set[int]]:
    """读缓存。返回 ``(阵容列表, 已抓过的 aid 集合)``。"""
    path = _cache_path()
    if not path.exists():
        return [], set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        logger.warning("B 站缓存损坏，忽略：%s", path)
        return [], set()
    lineups = payload.get("lineups") or []
    seen = {int(a) for a in (payload.get("articles") or [])}
    return lineups, seen


def _save_cache(lineups: list[dict], articles: set[int]) -> None:
    path = _cache_path()
    try:
        path.write_text(
            json.dumps(
                {"lineups": lineups, "articles": sorted(articles)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        logger.exception("写 B 站缓存失败：%s", path)


def scrape_lineups(
    limit: int | None = None,
    delay: float = VIEW_DELAY,
    resume: bool = True,
    progress=None,
) -> tuple[list[dict], str]:
    """抓 B 站专栏阵容。

    返回 ``(阵容列表, 错误说明)``。**限流中断时也返回已抓到的部分**
    （第二个返回值非空），与 ``bwiki.scrape_lineups`` 的约定一致。
    """
    lineups, done_articles = _load_cache() if resume else ([], set())
    if lineups:
        logger.info("从缓存恢复 %d 套阵容（%d 篇专栏）", len(lineups), len(done_articles))

    session = _new_session()
    try:
        articles = search_articles(session)
    except BiliError as exc:
        return lineups, str(exc)

    todo = [a for a in articles if a["id"] not in done_articles]
    if limit:
        todo = todo[:limit]
    logger.info("待抓取 %d 篇专栏（共发现 %d 篇）", len(todo), len(articles))

    error = ""
    for index, article in enumerate(todo, 1):
        aid = article["id"]
        try:
            text, meta = fetch_article_text(session, aid, delay)
            parsed = parse_lineup_codes(text, aid, fallback_title=article["title"])
            published = _to_date(meta.get("publish_time"))
            title = meta.get("title") or article["title"]

            for item in parsed:
                item["submitted_at"] = published
                item["author"] = meta.get("author") or ""
                item["article_title"] = title
                item["source_url"] = f"https://www.bilibili.com/read/cv{aid}/"
                item["lineup_type"] = _guess_type(title)
                item["intro"] = _excerpt(text)

            done_articles.add(aid)
            lineups.extend(parsed)
            # **每篇即存**：限流是常态，攒到最后一起写一次中断就全丢
            _save_cache(lineups, done_articles)

            if progress:
                progress(index, len(todo), f"cv{aid} +{len(parsed)} 套（{title[:16]}）")
        except BiliError as exc:
            error = str(exc)
            logger.warning("专栏 cv%d 抓取失败：%s", aid, exc)
        except Exception as exc:  # noqa: BLE001 - 单篇失败不该中断整批
            logger.warning("专栏 cv%d 解析异常：%s", aid, exc)
            error = f"部分失败：{exc}"

    _save_cache(lineups, done_articles)
    return lineups, error


def _guess_type(title: str) -> str:
    """从标题猜 pvp / pve。

    默认 pvp：B 站配队分享绝大多数是对战配队。只有标题明确写 PVE
    相关词才判 pve。**猜错的代价小于编造**——阵容码本身不带类型字段。
    """
    lowered = title.lower()
    for hint in ("pve", "抓捕", "刷图", "副本", "挂机"):
        if hint in lowered:
            return "pve"
    return "pvp"


def _excerpt(text: str, limit: int = 200) -> str:
    """取正文里第一段有内容的说明文字，作为阵容简介。

    跳过 ``#`` 开头的阵容码行和 ``###`` 标题行——那些是结构化字段，
    不是"介绍"。
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("阵容码"):
            continue
        if _CODE_RE.match(line):
            continue
        if len(line) >= 12:
            return line[:limit]
    return ""
