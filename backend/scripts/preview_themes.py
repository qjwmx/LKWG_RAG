"""预览 daisyUI 内置主题：注入主题 CSS 后逐张截图。

为什么不在源码里切换主题来预览
------------------------------
daisyUI 5 是 **tree-shaken** 的：只有出现在源码里的类才会进产物。
主题变量同理——只启用 light/dark 时，`data-theme="nord"` 不会有任何效果。

所以这里**在运行时注入**主题 CSS（从 node_modules 读原文件），
不改动仓库里任何文件。这既符合"先看再改"的流程，
也避免为了预览在 style.css 里反复增删主题名。

用法::

    python scripts/preview_themes.py --url http://127.0.0.1:5173/strategy
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.shot import Cdp, find_browser, start_browser, wait_for_devtools  # noqa: E402

THEME_DIR = Path(__file__).resolve().parent.parent.parent / "frontend" / "node_modules" / "daisyui" / "theme"


def login_token() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "admin"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(opener.open(req, timeout=30).read())["data"]["access_token"]


def theme_css(name: str) -> str:
    path = THEME_DIR / f"{name}.css"
    if not path.exists():
        raise SystemExit(f"没有这个主题：{name}")
    return path.read_text(encoding="utf-8")


async def run(ws_url: str, url: str, token: str, themes: list[str], out_dir: Path,
              width: int, height: int, settle: float, scroll: int) -> None:
    import websockets

    out_dir.mkdir(parents=True, exist_ok=True)

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024, proxy=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       width=width, height=height, deviceScaleFactor=1, mobile=False)
        await cdp.call("Emulation.setEmulatedMedia",
                       features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])

        await cdp.call("Page.navigate", url="http://127.0.0.1:5173/login")
        await asyncio.sleep(3)
        await cdp.call("Runtime.evaluate",
                       expression=f"localStorage.setItem('rag_token', {json.dumps(token)})")

        for name in themes:
            css = theme_css(name).replace("\\", "\\\\").replace("`", "\\`")
            # 注入主题规则 + 设 data-theme。主题规则的选择器是 [data-theme="x"]，
            # 所以设上属性就会生效。
            js = f"""
            (() => {{
              document.getElementById('__preview_theme__')?.remove();
              const s = document.createElement('style');
              s.id = '__preview_theme__';
              s.textContent = `{css}`;
              document.head.appendChild(s);
              document.documentElement.setAttribute('data-theme', {json.dumps(name)});
              return document.documentElement.getAttribute('data-theme');
            }})()
            """
            # 每次导航后都要重注入（导航会清掉注入的节点）
            await cdp.call("Page.navigate", url=url)
            await asyncio.sleep(settle)
            got = await cdp.call("Runtime.evaluate", expression=js, returnByValue=True)
            await asyncio.sleep(1.2)

            if scroll:
                await cdp.call("Runtime.evaluate",
                               expression=f"window.scrollTo(0, {scroll});"
                                          f"document.querySelector('.scrollbar-slim')?.scrollTo(0,{scroll});")
                await asyncio.sleep(0.6)

            shot = await cdp.call("Page.captureScreenshot", format="png")
            path = out_dir / f"{name}.png"
            path.write_bytes(base64.b64decode(shot["data"]))
            print(f"  {name:<12} theme={got['result'].get('value')}  -> {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173/strategy")
    parser.add_argument("--out", default="preview")
    parser.add_argument("--themes", default="light,dark,wireframe,lofi,nord,business,night")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--settle", type=float, default=5.0)
    parser.add_argument("--scroll", type=int, default=0)
    args = parser.parse_args()

    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-preview-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)

    proc, port = start_browser(browser, args.width, args.height, profile)
    try:
        asyncio.run(
            run(wait_for_devtools(port), args.url, login_token(), themes,
                Path(args.out), args.width, args.height, args.settle, args.scroll)
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
