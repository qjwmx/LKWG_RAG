"""给毛玻璃 + 壁纸拍一组截图，供人工核对。

为什么需要脚本而不是手敲命令行
-----------------------------
要设置的 localStorage 值里含 JSON（双引号、花括号），
在 PowerShell 里传给 `--eval` 会被反复转义、极难写对。
写进 Python 字符串里则一目了然，也方便一次拍多页多主题。

用法::

    python -m scripts.shot_glass --out _shot
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


def login_token() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "admin"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(opener.open(req, timeout=30).read())["data"]["access_token"]


async def run(ws_url: str, token: str, out_dir: Path, width: int, height: int,
              preset: str, opacity: float) -> None:
    import websockets

    out_dir.mkdir(parents=True, exist_ok=True)

    pages = [
        ("strategy", "http://127.0.0.1:5173/strategy"),
        ("chat", "http://127.0.0.1:5173/chat"),
        ("knowledge", "http://127.0.0.1:5173/knowledge"),
    ]

    async with websockets.connect(ws_url, max_size=128 * 1024 * 1024, proxy=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       width=width, height=height, deviceScaleFactor=1, mobile=False)
        # headless 默认 prefers-reduced-motion: reduce，会让页面走进
        # 无障碍分支（动画全关、部分元素隐藏），截图与实际观感不符。
        await cdp.call("Emulation.setEmulatedMedia",
                       features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])

        await cdp.call("Page.navigate", url="http://127.0.0.1:5173/login")
        await asyncio.sleep(3)
        await cdp.call("Runtime.evaluate",
                       expression=f"localStorage.setItem('rag_token', {json.dumps(token)})")

        for theme in ("lofi", "business"):
            wp = json.dumps({
                "source": "preset",
                "presetId": preset,
                "opacity": opacity,
            })
            await cdp.call("Runtime.evaluate", expression=(
                f"localStorage.setItem('rag_theme', {json.dumps(theme)});"
                f"localStorage.setItem('rag_wallpaper', {json.dumps(wp)});"
            ))

            for name, url in pages:
                await cdp.call("Page.navigate", url=url)
                await asyncio.sleep(6)
                shot = await cdp.call("Page.captureScreenshot", format="png")
                path = out_dir / f"{name}-{theme}.png"
                path.write_bytes(base64.b64decode(shot["data"]))
                print(f"  {path.name}")

        # 登录页单独拍。
        # ★ 必须先**清掉 token**：带着有效 token 访问 /login 会被路由守卫
        # 直接重定向到 /strategy，于是"登录页截图"其实是攻略页
        # （第一次跑就踩了这个，两张图一模一样）。
        for theme in ("lofi", "business"):
            wp = json.dumps({"source": "preset", "presetId": preset, "opacity": opacity})
            await cdp.call("Runtime.evaluate", expression=(
                "localStorage.removeItem('rag_token');"
                f"localStorage.setItem('rag_theme', {json.dumps(theme)});"
                f"localStorage.setItem('rag_wallpaper', {json.dumps(wp)});"
            ))
            await cdp.call("Page.navigate", url="http://127.0.0.1:5173/login")
            await asyncio.sleep(6)
            shot = await cdp.call("Page.captureScreenshot", format="png")
            path = out_dir / f"login-{theme}.png"
            path.write_bytes(base64.b64decode(shot["data"]))
            print(f"  {path.name}")

        # 恢复 token，后面还要拍登录后的页面
        await cdp.call("Runtime.evaluate",
                       expression=f"localStorage.setItem('rag_token', {json.dumps(token)})")

        # 壁纸选择器打开的状态：验证浮层玻璃与缩略图
        await cdp.call("Page.navigate", url="http://127.0.0.1:5173/strategy")
        await asyncio.sleep(6)
        await cdp.call("Runtime.evaluate",
                       expression="document.querySelector('.dropdown > summary')?.click(); true")
        await asyncio.sleep(1.2)
        shot = await cdp.call("Page.captureScreenshot", format="png")
        path = out_dir / "picker-open.png"
        path.write_bytes(base64.b64decode(shot["data"]))
        print(f"  {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="_shot")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--preset", default="mesh")
    parser.add_argument("--opacity", type=float, default=0.55)
    args = parser.parse_args()

    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-shotglass-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, args.width, args.height, profile)
    try:
        asyncio.run(run(wait_for_devtools(port), login_token(), Path(args.out),
                        args.width, args.height, args.preset, args.opacity))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
