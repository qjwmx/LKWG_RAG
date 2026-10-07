"""检查 /strategy 页面是否有运行时错误。

起因：diag_wallpaper_upload 报告 `.wallpaper` 元素不存在、
`--wallpaper-image` 为空——但同一时刻 probe_glass 跑得好好的。
"同一页面，一个探针说正常、另一个说没渲染" 只有两种可能：
  a) 两个探针跑的时机/前置状态不同
  b) 页面确实偶发崩溃（Vue 运行时错误会让整棵树挂掉）

这个脚本把控制台错误、未捕获异常、以及关键元素是否存在
一次性列出来，避免继续在"猜"。

用法::

    python -m scripts.diag_page_state --url http://127.0.0.1:5173/strategy
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.shot import Cdp, find_browser, start_browser, wait_for_devtools  # noqa: E402

PROBE_JS = r"""
(() => {
  const root = document.documentElement;
  return JSON.stringify({
    url: location.pathname,
    title: document.title,
    appChildren: document.getElementById('app')?.children.length ?? -1,
    hasWallpaper: !!document.querySelector('.wallpaper'),
    hasCanvas: !!document.querySelector('.glass-canvas'),
    hasChrome: !!document.querySelector('.glass-chrome'),
    hasRegion: !!document.querySelector('.glass-region'),
    hasPickerSummary: !!document.querySelector('.dropdown > summary'),
    hasFileInput: !!document.querySelector('input[type=file]'),
    varImage: getComputedStyle(root).getPropertyValue('--wallpaper-image').trim().slice(0, 60),
    theme: root.getAttribute('data-theme'),
    bodyText: (document.body.innerText || '').trim().slice(0, 120),
  });
})()
"""


def login_token() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "admin"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(opener.open(req, timeout=30).read())["data"]["access_token"]


async def run(ws_url: str, url: str, token: str, wait: float) -> int:
    import websockets

    logs: list[str] = []

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024, proxy=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Runtime.enable")
        await cdp.call("Log.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       width=1440, height=900, deviceScaleFactor=1, mobile=False)

        # 收控制台消息：不收集的话，Vue 的运行时错误只会出现在
        # DevTools 里，自动化跑的时候完全看不到。
        async def drain() -> None:
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                    msg = json.loads(raw)
                    method = msg.get("method")
                    if method == "Runtime.consoleAPICalled":
                        args = msg["params"].get("args", [])
                        text = " ".join(str(a.get("value", a.get("description", ""))) for a in args)
                        logs.append(f"[console.{msg['params'].get('type')}] {text[:200]}")
                    elif method == "Runtime.exceptionThrown":
                        d = msg["params"].get("exceptionDetails", {})
                        logs.append(f"[exception] {d.get('text')} {d.get('exception', {}).get('description', '')}"[:300])
                    elif method == "Log.entryAdded":
                        e = msg["params"]["entry"]
                        if e.get("level") in ("error", "warning"):
                            logs.append(f"[log.{e['level']}] {e.get('text', '')[:200]}")
            except (asyncio.TimeoutError, Exception):
                return

        await cdp.call("Page.navigate", url="http://127.0.0.1:5173/login")
        await asyncio.sleep(3)
        await cdp.call("Runtime.evaluate",
                       expression=f"localStorage.setItem('rag_token', {json.dumps(token)})")

        await cdp.call("Page.navigate", url=url)
        await asyncio.sleep(wait)
        await drain()

        raw = (await cdp.call("Runtime.evaluate", expression=PROBE_JS,
                              returnByValue=True))["result"]["value"]
        state = json.loads(raw) if isinstance(raw, str) else raw

        print("=" * 72)
        print(f"页面状态：{url}")
        print("=" * 72)
        for k, v in state.items():
            print(f"  {k:<18} {v!r}")

        print()
        print("控制台消息：")
        if logs:
            for line in logs[:30]:
                print(f"  {line}")
        else:
            print("  （无 error/warning）")

        problems = []
        if state["appChildren"] <= 0:
            problems.append("Vue 没有挂载任何内容（#app 是空的）")
        if not state["hasWallpaper"]:
            problems.append(".wallpaper 元素不存在")
        if not state["hasPickerSummary"]:
            problems.append("壁纸按钮不存在")
        if not state["varImage"]:
            problems.append("--wallpaper-image 为空（useWallpaper 的 apply() 没跑）")

        print()
        if problems:
            print("发现 %d 个问题：" % len(problems))
            for p in problems:
                print(f"  - {p}")
            return 1
        print("页面渲染正常")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173/strategy")
    parser.add_argument("--wait", type=float, default=6.0)
    args = parser.parse_args()

    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-pstate-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, 1440, 900, profile)
    try:
        return asyncio.run(run(wait_for_devtools(port), args.url, login_token(), args.wait))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
