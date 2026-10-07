"""诊断「盒内最差」为何在带边框元素上误报。

现象
----
probe_glass 报告数据来源提示条（`border-base-content/10 ink-muted ... border`）
的"盒内最差"是 1.36:1，但中位数判定是达标的。

怀疑
----
采样盒就是元素自己的包围盒，而元素**自带 1px 边框**。
一条横向细边框在"宽而扁"的盒子里占比很高：
一个 1000×28 的盒子，上下各 1 行边框 = 2/28 ≈ 7% 的像素，
正好落在 5% 分位上——于是"盒内最差"量到的是**元素自己的边框**，
不是文字背后的底色。边框颜色是 base-content/10（很深），
所以算出来的对比度极低。

这类误报的危险：它会让真正的局部问题淹没在噪声里，
最后没人再认真看这个提示。

用法::

    python -m scripts.diag_local_worst
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.shot import Cdp, find_browser, start_browser, wait_for_devtools  # noqa: E402
from scripts.probe_glass import (COLLECT_JS, HIDE_TEXT_JS, RESTORE_TEXT_JS,  # noqa: E402
                                 sample_backdrop)

TARGET = "阵容数据来自"


def login_token() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "admin"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(opener.open(req, timeout=30).read())["data"]["access_token"]


async def shot(cdp) -> bytes:
    r = await cdp.call("Page.captureScreenshot", format="png")
    return base64.b64decode(r["data"])


async def run(ws_url: str, url: str, token: str) -> int:
    import websockets
    from PIL import Image

    async with websockets.connect(ws_url, max_size=128 * 1024 * 1024, proxy=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       width=1440, height=900, deviceScaleFactor=1, mobile=False)
        await cdp.call("Emulation.setEmulatedMedia",
                       features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])
        await cdp.call("Page.navigate", url="http://127.0.0.1:5173/login")
        await asyncio.sleep(3)
        await cdp.call("Runtime.evaluate",
                       expression=f"localStorage.setItem('rag_token', {json.dumps(token)})")
        await cdp.call("Runtime.evaluate", expression=(
            "localStorage.setItem('rag_theme','lofi');"
            "localStorage.setItem('rag_wallpaper','{\"source\":\"none\",\"presetId\":\"indigo\",\"opacity\":0.35}');"
        ))
        await cdp.call("Page.navigate", url=url)
        await asyncio.sleep(6)

        # 打开壁纸选择器：1.36 这个值只在 --open-picker 下出现，
        # 怀疑是浮层面板**盖住**了提示条的一部分，
        # 于是采样盒里混进了面板自身的像素（边框/阴影/缩略图），
        # 量出来的"底色"根本不是提示条的底色。
        await cdp.call("Runtime.evaluate",
                       expression="document.querySelector('.dropdown > summary')?.click(); true")
        await asyncio.sleep(1.2)
        panel_raw = (await cdp.call("Runtime.evaluate", expression=(
            "(() => { const p = document.querySelector('.dropdown-content');"
            "if (!p) return 'null'; const r = p.getBoundingClientRect();"
            "return JSON.stringify({x:Math.round(r.left),y:Math.round(r.top),"
            "w:Math.round(r.width),h:Math.round(r.height)}); })()"
        ), returnByValue=True))["result"]["value"]
        print(f"选择器面板 rect = {panel_raw}")
        print()

        items = json.loads((await cdp.call("Runtime.evaluate", expression=COLLECT_JS,
                                           returnByValue=True))["result"]["value"])
        matches = [it for it in items if it["text"].startswith(TARGET)]
        if not matches:
            print("未找到目标元素")
            return 1
        it = matches[0]

        await cdp.call("Runtime.evaluate", expression=HIDE_TEXT_JS)
        await asyncio.sleep(0.4)
        img = Image.open(io.BytesIO(await shot(cdp))).convert("RGB")
        await cdp.call("Runtime.evaluate", expression=RESTORE_TEXT_JS)

        print(f"元素: {it['text'][:30]!r}")
        print(f"  rect = x={it['x']:.0f} y={it['y']:.0f} w={it['w']:.0f} h={it['h']:.0f}")
        print(f"  cls  = {it['cls']}")
        print(f"  color= {it['rgba']}")
        print()

        px = img.load()
        print("逐行扫描该盒子的底色（每行取中间一点的像素）：")
        left = int(it["x"])
        right = int(it["x"] + it["w"])
        mid = (left + right) // 2
        top = int(it["y"])
        bottom = int(it["y"] + it["h"])
        for y in range(max(0, top - 2), min(img.height, bottom + 3)):
            p = px[mid, y]
            mark = ""
            if y < top or y >= bottom:
                mark = "  <- 盒外"
            elif y == top or y == bottom - 1:
                mark = "  <- 边框行"
            print(f"    y={y:4}  rgb={p}{mark}")

        print()
        median_bg, samples = sample_backdrop(img, it["x"], it["y"], it["w"], it["h"])
        print(f"中位数底色 = {median_bg}")
        ordered = sorted(samples, key=lambda c: sum(c))
        print(f"最暗的 5 个采样 = {ordered[:5]}")
        print(f"最亮的 5 个采样 = {ordered[-5:]}")
        dark_share = sum(1 for c in samples if sum(c) < 400) / len(samples)
        print(f"很暗（sum<400）的采样占比 = {dark_share * 100:.1f}%")
        print()
        print("若暗像素占比约等于 2/高度，就说明量到的是元素自己的边框。")
        print(f"  2/高度 = {2 / it['h'] * 100:.1f}%")
    return 0


def main() -> int:
    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-lw-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, 1440, 900, profile)
    try:
        return asyncio.run(run(wait_for_devtools(port),
                               "http://127.0.0.1:5173/strategy", login_token()))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
