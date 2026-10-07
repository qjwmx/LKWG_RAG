"""用 CDP 驱动 Chrome 截图，并**关掉 headless 默认的 reduced-motion 模拟**。

为什么需要这个脚本
------------------
headless Chrome 会把 ``prefers-reduced-motion`` 报成 ``reduce``
（已实测确认）。登录页的右侧展示有一条无障碍规则：检测到 reduced-motion
时隐藏两侧的卡、只留中间那张。于是截图里永远只有一张精灵，
看起来像"封面流没生效"——实际上是被无障碍分支正确命中了。

用 ``--force-prefers-reduced-motion`` 命令行开关**无效**（实测），
只能走 CDP 的 ``Emulation.setEmulatedMedia``。所以这个脚本用
WebSocket 直接跟 DevTools 协议说话。

用法::

    python -m scripts.shot --url http://127.0.0.1:5173/login --out shot.png
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_browser() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("chrome") or shutil.which("msedge")
    if found:
        return found
    raise SystemExit("找不到 Chrome/Edge")


# Chrome 会**静默拒绝**某些远程调试端口。实测：动态选出的高位端口
# （如 49152+ 区间）Chrome 不监听也不报错，进程照常活着，
# 表现为"DevTools 未就绪：连接被拒绝"，很容易误判成 Chrome 没启动。
# 所以从一段确定可用的端口里依次尝试。
PORT_CANDIDATES = [9411, 9412, 9413, 9414, 9415]


def start_browser(browser: str, width: int, height: int, profile: Path):
    """启动 headless 浏览器，返回 (进程, 端口)。

    逐个试候选端口并**验证 DevTools 真的在监听**——不能只看进程活着。
    """
    opener = _devtools_opener()
    for port in PORT_CANDIDATES:
        proc = subprocess.Popen(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                # --no-sandbox 是**必须的**，不是可选项。
                # 从 Python 的 subprocess 启动时（与从 PowerShell 启动不同），
                # Chrome 的沙箱初始化会直接让进程以 0x80000003 退出，
                # 而且 stderr 一个字都不输出——表现为"Chrome 起不来但没有任何线索"。
                # 实测：去掉它必崩，加上它必成。
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                f"--window-size={width},{height}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                    if json.loads(resp.read().decode("utf-8")):
                        return proc, port
            except Exception:  # noqa: BLE001 - 还没起来，继续等
                pass
            time.sleep(0.3)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    raise SystemExit("Chrome 未能在任何候选端口上开启 DevTools")


def _devtools_opener():
    """构造**绕过系统代理**的 urllib opener。

    这是 Windows 上必踩的坑：``urllib`` 会读取系统代理（来自注册表，
    环境变量里看不到），于是对 ``127.0.0.1`` 的请求被发进代理，
    表现为**连接超时**而不是拒绝——看起来像"Chrome 没起来"。
    ``requests`` 默认绕过 localhost，``urllib`` 和 ``httpx`` 都不会。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def wait_for_devtools(port: int, timeout: float = 40.0) -> str:
    """等 DevTools HTTP 端点起来，返回页面级的 WebSocket URL。"""
    opener = _devtools_opener()
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with opener.open(f"http://127.0.0.1:{port}/json/list", timeout=3) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except Exception as exc:  # noqa: BLE001 - 启动期连不上是正常的
            last = exc
        time.sleep(0.3)
    raise SystemExit(f"DevTools 未就绪：{last}")


class Cdp:
    """极简 CDP 客户端：发命令、等对应 id 的回复。"""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._next = 0

    async def call(self, method: str, **params):
        self._next += 1
        message_id = self._next
        await self._ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=60)
            data = json.loads(raw)
            if data.get("id") == message_id:
                if "error" in data:
                    raise RuntimeError(f"{method} 失败：{data['error']}")
                return data.get("result")


async def capture(
    ws_url: str,
    url: str,
    out: Path,
    width: int,
    height: int,
    reduced_motion: str,
    settle: float,
    token: str = "",
    eval_js: str = "",
    eval_wait: float = 0.0,
) -> None:
    import websockets

    # proxy=None 是**必须的**：websockets ≥ 15 会自动读取系统代理配置，
    # 而 Windows 上的代理来自注册表，回环地址也会被发进代理，
    # 表现为连接超时（看起来像 Chrome 没起来）。与 urllib 那个坑同源。
    async with websockets.connect(
        ws_url, max_size=64 * 1024 * 1024, proxy=None, open_timeout=30
    ) as ws:
        cdp = Cdp(ws)

        await cdp.call("Page.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       width=width, height=height, deviceScaleFactor=1, mobile=False)
        # 关键一步：显式声明"用户没有要求减少动态效果"，
        # 否则 headless 默认 reduce 会让页面走进无障碍分支
        await cdp.call(
            "Emulation.setEmulatedMedia",
            features=[{"name": "prefers-reduced-motion", "value": reduced_motion}],
        )

        # 先落到同源页面才能写 localStorage（about:blank 的 origin 是 opaque）
        if token:
            origin = url.split("/", 3)[:3]
            await cdp.call("Page.navigate", url="/".join(origin) + "/login")
            await asyncio.sleep(2.5)
            await cdp.call(
                "Runtime.evaluate",
                expression=f"localStorage.setItem('rag_token', {json.dumps(token)})",
            )

        await cdp.call("Page.navigate", url=url)
        # 等网络与首屏动画稳定。用固定等待而不是等 load 事件：
        # 图片是外链热链的，load 可能迟迟不来，而我们只想看首屏效果。
        await asyncio.sleep(settle)

        if eval_js:
            result = await cdp.call(
                "Runtime.evaluate", expression=eval_js, awaitPromise=True, returnByValue=True
            )
            value = (result.get("result") or {}).get("value")
            if value is not None:
                print("eval ->", value)
            # 截图时机很重要：SSE 是流式的，要在流还在跑的时候拍，
            # 否则进度面板已经让位给最终回答了。
            await asyncio.sleep(eval_wait)

        result = await cdp.call("Page.captureScreenshot", format="png")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(base64.b64decode(result["data"]))

        # 顺手把页面上的关键状态带回来，便于确认不是"看着像但其实没生效"
        probe = await cdp.call(
            "Runtime.evaluate",
            expression=(
                "JSON.stringify({reduce: matchMedia('(prefers-reduced-motion: reduce)').matches,"
                " slots: document.querySelectorAll('.slot').length,"
                " center: document.querySelectorAll('.slot-center').length,"
                " imgs: [...document.querySelectorAll('.slot-art')].map(i => i.naturalWidth),"
                " pipeline: !!document.querySelector('ol .dot, .loading-dots'),"
                " progress: [...document.querySelectorAll('[style*=width]')].length,"
                " messages: document.querySelectorAll('[data-role], .chat, article').length})"
            ),
            returnByValue=True,
        )
        print("probe:", probe["result"]["value"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--settle", type=float, default=6.0)
    parser.add_argument("--reduced-motion", default="no-preference")
    parser.add_argument("--token", default="", help="写入 localStorage 的 rag_token，用于需要登录的页面")
    parser.add_argument("--eval", dest="eval_js", default="", help="截图前在页面里执行的 JS")
    parser.add_argument("--eval-wait", type=float, default=0.0, help="执行 JS 后再等几秒才截图")
    args = parser.parse_args()

    browser = find_browser()
    # 用户数据目录放系统临时区，**绝不能放项目里**：
    # Vite 会监听整个项目目录，Chrome 在其中的缓存文件被锁住时
    # 会让 Vite 的 watcher 抛 EBUSY 直接崩掉（已踩过）。
    profile = Path(tempfile.gettempdir()) / f"rag-shot-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)

    proc, port = start_browser(browser, args.width, args.height, profile)

    try:
        ws_url = wait_for_devtools(port)
        asyncio.run(
            capture(
                ws_url,
                args.url,
                Path(args.out),
                args.width,
                args.height,
                args.reduced_motion,
                args.settle,
                token=args.token,
                eval_js=args.eval_js,
                eval_wait=args.eval_wait,
            )
        )
        print(f"saved: {args.out}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
