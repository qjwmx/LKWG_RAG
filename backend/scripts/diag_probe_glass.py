"""诊断 probe_glass 的取色是否正确。

起因：探针报告「洛」这个 logo（bg-primary + text-primary-content，理论上
对比度约 7:1）只有 1.14，且**在明暗两套主题下数值完全一样**。
两主题同值说明测量的是同一对颜色，与主题无关——这是"探针错了"的典型特征，
不是"样式错了"。

这个脚本把两件事摊开：
1. 元素在**正常截图**里的中心像素（应该能看到文字颜色）
2. 同一个点在**文字隐藏截图**里的中心像素（应该是纯底色）
若两者几乎相同，说明"文字隐藏"没生效，底色图其实还带着文字，
于是 fg 与 bg 被算成同一个东西 → 对比度必然接近 1。

用法::

    python -m scripts.diag_probe_glass
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
from scripts.probe_glass import COLLECT_JS, HIDE_TEXT_JS, RESTORE_TEXT_JS  # noqa: E402

# 挑几个已知答案的元素来对照
TARGETS = ["洛", "阵容与攻略", "属性筛选"]


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
            "localStorage.setItem('rag_wallpaper','{\"source\":\"none\",\"presetId\":\"aurora\",\"opacity\":0.35}');"
        ))
        await cdp.call("Page.navigate", url=url)
        await asyncio.sleep(6)

        collected = await cdp.call("Runtime.evaluate", expression=COLLECT_JS, returnByValue=True)
        items = json.loads(collected["result"]["value"])

        normal_png = await shot(cdp)
        await cdp.call("Runtime.evaluate", expression=HIDE_TEXT_JS)
        await asyncio.sleep(0.5)
        hidden_png = await shot(cdp)
        await cdp.call("Runtime.evaluate", expression=RESTORE_TEXT_JS)

        normal = Image.open(io.BytesIO(normal_png)).convert("RGB")
        hidden = Image.open(io.BytesIO(hidden_png)).convert("RGB")
        print(f"normal={normal.size}  hidden={hidden.size}")

        Path("diag-normal.png").write_bytes(normal_png)
        Path("diag-hidden.png").write_bytes(hidden_png)
        print("已保存 diag-normal.png / diag-hidden.png，可直接对照\n")

        for target in TARGETS:
            matches = [it for it in items if it["text"].strip() == target]
            if not matches:
                print(f"--- {target!r} 未找到 ---\n")
                continue
            for it in matches[:2]:
                cx = int(it["x"] + it["w"] / 2)
                cy = int(it["y"] + it["h"] / 2)
                cx = max(0, min(normal.width - 1, cx))
                cy = max(0, min(normal.height - 1, cy))
                print(f"--- {target!r}  cls={it['cls'][:60]}")
                print(f"    rect=({it['x']:.0f},{it['y']:.0f}) {it['w']:.0f}x{it['h']:.0f}"
                      f"  中心=({cx},{cy})")
                print(f"    computed color rgba={it['rgba']}  有效不透明度={it['alpha']}")
                print(f"    normal 中心像素 = {normal.getpixel((cx, cy))}")
                print(f"    hidden 中心像素 = {hidden.getpixel((cx, cy))}")
                same = normal.getpixel((cx, cy)) == hidden.getpixel((cx, cy))
                print(f"    两图中心是否相同 = {same}"
                      f"{'   <<< 文字没被隐藏，底色图仍带文字' if same else ''}")
                print()

        # 全局：两张图有多少像素不同？如果几乎相同，说明隐藏没生效
        diff = 0
        total = 0
        for y in range(0, normal.height, 4):
            for x in range(0, normal.width, 4):
                total += 1
                if normal.getpixel((x, y)) != hidden.getpixel((x, y)):
                    diff += 1
        print(f"两图差异像素：{diff}/{total}（{diff / total * 100:.1f}%）")
        print("  若这个比例接近 0，说明 HIDE_TEXT_JS 没生效；"
              "正常应在 3%~15% 之间（只有文字像素变了）")

        # 关键：hidden 图里还有没有纯黑/纯白的文字像素？
        dark = sum(1 for y in range(0, hidden.height, 4) for x in range(0, hidden.width, 4)
                   if sum(hidden.getpixel((x, y))) < 150)
        print(f"hidden 图中很暗的像素数（采样）：{dark}")
    return 0


def main() -> int:
    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-diag-{os.getpid()}"
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
