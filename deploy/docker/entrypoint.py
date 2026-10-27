#!/usr/bin/env python
"""容器启动前的初始化（幂等）。

容器和宿主机部署有一个关键差异：**镜像里没有运行时数据库**。
``backend/data/`` 被挂成卷，第一次启动时是空的，于是图鉴、阵容、属性克制
这些领域表一条数据都没有——前端页面能打开，但精灵列表是空的，
看起来像"部署失败"，实际只是没导种子数据。

本脚本做两件事，都是幂等的：

1. 建表（``init_db``，与后端启动时做的是同一件事，重复执行无副作用）；
2. **只在领域表为空时**导入种子数据。

为什么必须判断"为空"而不是每次都导：``seed_loader`` 的实现是
"先清表再写"，每次重启都跑一遍会把爬虫抓来的新阵容一起删掉——
那是最不该发生的数据丢失。所以这里只在首启动时导一次。

用法（Dockerfile 的 ENTRYPOINT）::

    python /app/deploy/docker/entrypoint.py && exec uvicorn ...
"""

from __future__ import annotations

import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("entrypoint")


def main() -> int:
    from app.db.session import init_db

    init_db()
    logger.info("数据库表已就绪")

    try:
        from app.services import roco_store
    except Exception as exc:  # noqa: BLE001 - 初始化失败要给出明确原因
        logger.error("无法加载领域数据模块：%s: %s", type(exc).__name__, exc)
        return 1

    try:
        status = roco_store.stats()
    except Exception as exc:  # noqa: BLE001
        logger.error("读取领域数据统计失败：%s: %s", type(exc).__name__, exc)
        return 1

    if status.get("pets", 0) > 0 and status.get("lineups_total", 0) > 0:
        logger.info(
            "领域数据已存在（精灵 %s / 阵容 %s），跳过种子导入",
            status.get("pets"),
            status.get("lineups_total"),
        )
        return 0

    logger.info("领域表为空，导入种子数据（首次启动，约需 10~60 秒）")
    try:
        from app.scraper.seed_loader import seed_all

        result = seed_all()
    except Exception as exc:  # noqa: BLE001 - 种子导入失败不该让容器起不来
        # **不退出**：后端接口与知识库功能仍然可用，只是图鉴/阵容页空着。
        # 让容器起来，运维才能通过日志与页面判断问题，比直接 CrashLoop 好。
        logger.error("种子数据导入失败（服务仍会启动，图鉴/阵容页会是空的）：%s: %s",
                     type(exc).__name__, exc)
        return 0

    logger.info("种子数据导入完成：%s", result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
