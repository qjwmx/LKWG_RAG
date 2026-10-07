"""测量「壁纸的结构细节」有多少能透到屏幕上。

为什么之前的探针没抓到这个问题
-----------------------------
`probe_wallpaper_visible.py` 比较的是"有壁纸 / 无壁纸"两张图的**平均色差**。
平均值能通过，但用户看到的可能只是一片**没有细节的色晕**——
平均色差只说明"颜色变了"，不说明"能不能认出这是一张图"。

用户的反馈正是这样：毛玻璃质感没问题，但壁纸"看不见"。

怀疑的根因
----------
`backdrop-filter: blur()` 会模糊**元素背后的一切**。而本项目里
`.glass-region` 铺满整个内容区、`.glass-chrome` 铺满顶栏与侧栏——
**整个视口都被一层模糊盖住了**，所以壁纸在任何地方都不清晰。

测量方法
--------
往壁纸层注入一张**已知尺寸的棋盘格**（纯红/纯蓝交替），然后：
  1. 取"只有玻璃、没有被卡片或顶栏盖住"的采样点
  2. 量这些点上 `R−B` 的**标准差**
  3. 棋盘格的理论值是 ±255，被模糊混色后幅度会塌缩
  4. 再统计"仍然纯色"（|R−B| > 150）的采样点比例 → **清晰度**

棋盘格是刻意的：它把"结构"变成一个可直接测量的数字。
80px 的格子都看不清，照片就更不可能认出来。

对比三种配置，定位到底是谁在糊壁纸：
  A. 当前（region 有 blur）
  B. region 不模糊
  C. region 完全去掉

用法::

    python -m scripts.probe_wallpaper_detail
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import shutil
import statistics
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.shot import Cdp, find_browser, start_browser, wait_for_devtools  # noqa: E402

CELL = 80

# 注入棋盘格。用 CSS 变量直接写，绕过上传路径——
# 这里要测的是**渲染**，不是上传。
PATTERN_JS = r"""
(() => {
  const CELL = __CELL__;
  const c = document.createElement('canvas');
  c.width = 1200; c.height = 800;
  const x = c.getContext('2d');
  for (let i = 0; i < Math.ceil(c.width / CELL); i++) {
    for (let j = 0; j < Math.ceil(c.height / CELL); j++) {
      x.fillStyle = (i + j) % 2 === 0 ? '#ff0000' : '#0000ff';
      x.fillRect(i * CELL, j * CELL, CELL, CELL);
    }
  }
  const url = c.toDataURL('image/png');
  const root = document.documentElement;
  root.style.setProperty('--wallpaper-image', 'url("' + url + '")');
  root.style.setProperty('--wallpaper-opacity', '0.75');
  return JSON.stringify({ ok: true });
})()
"""

# 收集"会盖住壁纸"的元素包围盒，以及内容区范围。
# 采样点必须落在既不属于内容区之外、也不被任何面板覆盖的位置。
GEOM_JS = r"""
(() => {
  const rects = (sel) => [...document.querySelectorAll(sel)]
    .map((el) => el.getBoundingClientRect())
    .filter((b) => b.width > 8 && b.height > 8)
    .map((b) => ({ left: b.left, top: b.top, right: b.right, bottom: b.bottom }));

  // 所有"有毛玻璃"或"有填充"的面板都算遮挡物
  const blockers = [
    ...rects('.surface'),
    ...rects('.glass-chrome'),
    ...rects('.glass-panel'),
    ...rects('.dropdown-content'),
    ...rects('.modal-box'),
  ];

  // 内容区：取最大的那个 .glass-region
  let region = null;
  let best = 0;
  for (const el of document.querySelectorAll('.glass-region')) {
    const b = el.getBoundingClientRect();
    if (b.width * b.height > best) { best = b.width * b.height; region = b; }
  }
  if (!region) return JSON.stringify({ error: '找不到 .glass-region' });

  return JSON.stringify({
    region: { left: region.left, top: region.top, right: region.right, bottom: region.bottom },
    blockers,
  });
})()
"""

OVERRIDE_JS = r"""
(() => {
  document.getElementById('__probe_override__')?.remove();
  const css = __CSS__;
  if (css) {
    const s = document.createElement('style');
    s.id = '__probe_override__';
    s.textContent = css;
    document.head.appendChild(s);
  }
  return true;
})()
"""

CONFIGS: list[tuple[str, str]] = [
    ("A 当前（region 有 blur）", ""),
    ("B region 不模糊", ".glass-region{backdrop-filter:none!important;"
                        "-webkit-backdrop-filter:none!important}"),
    ("C region 完全去掉", ".glass-region{backdrop-filter:none!important;"
                          "-webkit-backdrop-filter:none!important;"
                          "background-color:transparent!important}"),
]


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


def in_any(rects: list[dict], x: float, y: float) -> bool:
    for r in rects:
        if r["left"] <= x <= r["right"] and r["top"] <= y <= r["bottom"]:
            return True
    return False


async def run(ws_url: str, url: str, token: str, keep: bool, out_dir: Path) -> int:
    import websockets
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)

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
            "localStorage.setItem('rag_wallpaper',"
            "'{\"source\":\"preset\",\"presetId\":\"indigo\",\"opacity\":0.75}');"
        ))
        await cdp.call("Page.navigate", url=url)
        await asyncio.sleep(6)

        await cdp.call("Runtime.evaluate",
                       expression=PATTERN_JS.replace("__CELL__", str(CELL)))
        await asyncio.sleep(1.0)

        geom = json.loads((await cdp.call("Runtime.evaluate", expression=GEOM_JS,
                                          returnByValue=True))["result"]["value"])
        if "error" in geom:
            print(geom["error"])
            return 1

        region = geom["region"]
        blockers = geom["blockers"]

        # 在内容区里按 20px 网格撒点，剔除被面板盖住的。
        # 20px 远小于 80px 的格子，所以一定能同时采到红格与蓝格。
        pts: list[tuple[int, int]] = []
        y = int(region["top"]) + 6
        while y < int(region["bottom"]) - 6:
            x = int(region["left"]) + 6
            while x < int(region["right"]) - 6:
                if not in_any(blockers, x, y):
                    pts.append((x, y))
                x += 20
            y += 20

        print(f"内容区 x=[{region['left']:.0f},{region['right']:.0f}] "
              f"y=[{region['top']:.0f},{region['bottom']:.0f}]")
        print(f"遮挡面板 {len(blockers)} 个；可用采样点 {len(pts)} 个")
        if len(pts) < 20:
            print("可用采样点太少——内容区几乎被卡片填满，"
                  "壁纸本来就只能在很窄的缝隙里出现")
        print()
        # 用 ASCII 的 "-" 而不是 U+2212 的 "−"：Windows 控制台默认 GBK，
        # 打印 U+2212 会抛 UnicodeEncodeError，而且是在**打印表头时**抛，
        # 把真正的探测结果盖掉，看起来像"探针本身坏了"。
        print(f"{'配置':<26}{'R-B 标准差':>12}{'纯色点比例':>12}   判定")
        print("-" * 70)

        for label, css in CONFIGS:
            await cdp.call("Runtime.evaluate",
                           expression=OVERRIDE_JS.replace("__CSS__", json.dumps(css)))
            await asyncio.sleep(0.8)
            png = await shot(cdp)
            if keep:
                (out_dir / f"detail-{label[0]}.png").write_bytes(png)
            img = Image.open(io.BytesIO(png)).convert("RGB")
            px = img.load()

            diffs: list[int] = []
            pure = 0
            for (x, y2) in pts:
                p = px[x, y2]
                d = int(p[0]) - int(p[2])
                diffs.append(d)
                if abs(d) > 150:
                    pure += 1

            sd = statistics.pstdev(diffs) if len(diffs) > 1 else 0.0
            pure_ratio = pure / len(pts) if pts else 0.0
            verdict = ("结构清晰" if pure_ratio >= 0.5 else
                       "可辨认" if pure_ratio >= 0.25 else
                       "★ 只剩色晕（看不出是图）")
            print(f"{label:<26}{sd:>12.1f}{pure_ratio * 100:>11.0f}%   {verdict}")

        print()
        print("说明：棋盘格是纯红/纯蓝交替，理论 |R-B| = 255。")
        print("      被模糊混色后幅度塌缩 → 标准差变小、纯色点比例下降。")
        print("      「纯色点比例」是直接指标：它表示有多少采样点仍然")
        print("      保持原图的纯色，即用户能否看出这是图案。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173/strategy")
    parser.add_argument("--out", default="_shot")
    parser.add_argument("--keep-images", action="store_true")
    args = parser.parse_args()

    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-detail-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, 1440, 900, profile)
    try:
        return asyncio.run(run(wait_for_devtools(port), args.url, login_token(),
                               args.keep_images, Path(args.out)))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
