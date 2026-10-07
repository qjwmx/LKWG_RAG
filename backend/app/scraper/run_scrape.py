"""爬虫命令行入口（多数据源）。

用法::

    # 看有哪些源
    python -m app.scraper.run_scrape --list-sources

    # BWIKI：抓 30 套试水（推荐先这样验证网络是否被拦）
    python -m app.scraper.run_scrape --source bwiki --limit 30

    # B 站：抓当赛季的「阵容码」配队
    python -m app.scraper.run_scrape --source bilibili --limit 10

    # 继续上次中断的抓取
    python -m app.scraper.run_scrape --source bwiki --resume

    # 调大间隔（被反爬拦了就调这个）
    python -m app.scraper.run_scrape --source bwiki --delay 5

    # 把抓到的写进数据库（按源）
    python -m app.scraper.run_scrape --import bwiki
    python -m app.scraper.run_scrape --import bilibili
    python -m app.scraper.run_scrape --import all

设计取向：**默认只抓不写库**。抓取与入库分开，是因为抓取很容易
半途被反爬打断——先把原始数据落到 ``data/_scrape_cache/``，
确认完整了再显式 ``--import`` 写库，避免半份数据污染知识库。
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.db.session import init_db

# 数据源注册表：名字 -> (显示名, 模块路径, 默认间隔秒)
SOURCES: dict[str, tuple[str, str, float]] = {
    "bwiki": ("BWIKI 玩家投稿（精灵阵容页）", "app.scraper.bwiki", 2.5),
    "bilibili": ("B 站专栏（阵容码配队）", "app.scraper.bilibili", 3.0),
}


def _print(text: str = "") -> None:
    """安全打印。

    Windows 控制台默认是 GBK（cp936），直接 ``print("⚠ ...")`` 会抛
    ``UnicodeEncodeError`` —— 而且是在**打印错误信息时**抛，把真正的
    失败原因盖掉，用户只看到一段 traceback。这里降级成 ASCII 而不是
    崩掉，因为输出是给人看的提示，不该影响退出码与已保存的数据。
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, errors="replace").decode(encoding), flush=True)


def _load_module(name: str):
    from importlib import import_module

    return import_module(SOURCES[name][1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_scrape",
        description="抓取《洛克王国：世界》的阵容数据（支持多个数据源）",
    )
    parser.add_argument(
        "--source",
        default="bwiki",
        choices=sorted(SOURCES),
        help="数据源（默认 bwiki）",
    )
    parser.add_argument("--limit", type=int, default=30, help="抓取多少套（默认 30，0 表示全部）")
    parser.add_argument("--delay", type=float, default=None, help="请求间隔秒数（默认按源而定）")
    parser.add_argument("--resume", action="store_true", help="从缓存继续（跳过已抓的）")
    parser.add_argument(
        "--import",
        dest="import_source",
        default="",
        help="把缓存写进数据库后退出（bwiki / bilibili / all）",
    )
    parser.add_argument("--list-sources", action="store_true", help="列出可用数据源后退出")
    parser.add_argument("--quiet", action="store_true", help="只输出进度，不打日志")
    args = parser.parse_args(argv)

    if args.list_sources:
        _print("可用数据源：")
        for name, (label, module, delay) in sorted(SOURCES.items()):
            _print(f"  {name:<10} {label}（默认间隔 {delay}s，{module}）")
        return 0

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.import_source:
        return _import(args.import_source)

    label, _module, default_delay = SOURCES[args.source]
    delay = args.delay if args.delay is not None else default_delay

    from app.scraper import importer

    scraper = _load_module(args.source)

    def progress(done: int, total: int, text: str) -> None:
        _print(f"[{done}/{total}] {text}")

    _print(f"数据源：{label}")
    _print(
        f"开始抓取（间隔 {delay}s，"
        f"{'续传' if args.resume else '全新'}）。被反爬拦截时会自动保存已抓部分。\n"
    )
    lineups, error = scraper.scrape_lineups(
        limit=args.limit or None,
        delay=delay,
        resume=args.resume,
        progress=progress,
    )

    _print(f"\n共抓到 {len(lineups)} 套阵容。")
    if error:
        _print(f"\n[!] 中断原因：{error}")
        _print("已抓到的数据已保存到 data/_scrape_cache/，")
        _print("等待几分钟后用 --resume 继续。")

    cache_name = importer.SOURCE_CACHE.get(args.source, "lineups.json")
    _print(f"\n缓存：data/_scrape_cache/{cache_name}")
    _print(f"写入数据库：python -m app.scraper.run_scrape --import {args.source}")
    return 2 if error else 0


def _import(source: str) -> int:
    """把缓存写进数据库。``source='all'`` 时按注册顺序导入全部。"""
    from app.scraper import importer

    init_db()

    targets = sorted(importer.SOURCE_CACHE) if source == "all" else [source]
    total_inserted = total_updated = 0
    failures = 0

    for name in targets:
        try:
            inserted, updated, skipped = importer.import_source(name)
        except importer.ImportError_ as exc:
            _print(f"[{name}] 跳过：{exc}")
            failures += 1
            continue
        _print(f"[{name}] 新增 {inserted} 套，更新 {updated} 套，跳过 {skipped} 条。")
        total_inserted += inserted
        total_updated += updated

    if source == "all":
        _print(f"\n合计：新增 {total_inserted} 套，更新 {total_updated} 套。")

    # 全部源都失败才算失败；部分成功是有意义的（例如 B 站被限流但 BWIKI 有数据）
    return 1 if failures == len(targets) else 0


if __name__ == "__main__":
    sys.exit(main())
