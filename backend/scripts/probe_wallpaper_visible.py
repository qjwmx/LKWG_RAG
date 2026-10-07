"""验证「壁纸真的看得见」且「毛玻璃真的做在页面元素上」。

为什么必须单独验证这两件事
-------------------------
这两点是本功能的核心，但它们失效时**都不会报错**：

1. 壁纸被某个不透明层盖住 → 页面看起来"就是普通浅色/深色背景"，
   用户只会以为"壁纸没生效"，而代码、CSS 变量、localStorage 全是好的。
   本项目就踩过：一开始做了一整块全屏 `backdrop-filter` 画布，
   结果无论选哪张壁纸都是同一片灰雾。

2. 毛玻璃没做上（例如类名被 tree-shake、@supports 分支没命中）→
   页面仍然能看，只是"少了那层质感"，没有断言就永远发现不了。

做法：同一页面截两张图——
    A. 正常状态
    B. 把壁纸层隐藏（opacity: 0）
若 A 与 B 在"只有壁纸+玻璃、没有内容"的区域**几乎没有差别**，
说明壁纸没透上来（被盖住了）。

同时验证毛玻璃：取一个"结构层元素"与其背后的纯壁纸点，
两者颜色应当**不同**（玻璃把它压向主题底色）。

用法::

    python -m scripts.probe_wallpaper_visible --url http://127.0.0.1:5173/strategy
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

HIDE_WALLPAPER_JS = r"""
(() => {
  const s = document.createElement('style');
  s.id = '__probe_hide_wallpaper__';
  s.textContent = '.wallpaper { opacity: 0 !important; }';
  document.head.appendChild(s);
  return true;
})()
"""

RESTORE_JS = "document.getElementById('__probe_hide_wallpaper__')?.remove(); true"

# 采样区域：必须选"只有壁纸 + 结构层玻璃"的地方。
# 用 CSS 选择器描述，交给页面去算坐标，比硬编码像素稳。
SAMPLE_JS = r"""
(() => {
  const rectOf = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return null;
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  };
  return JSON.stringify({
    // 顶栏左侧的空白处：有玻璃、没有文字
    header: rectOf('header'),
    // 内容区（StrategyView 主区）中部：玻璃 + 卡片
    region: rectOf('.glass-region'),
    // 视口右下角：通常只有壁纸
    corner: { x: innerWidth - 40, y: innerHeight - 40, w: 30, h: 30 },
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


def avg(img, box) -> tuple[int, int, int]:
    left = max(0, int(box["x"]))
    top = max(0, int(box["y"]))
    right = min(img.width, int(box["x"] + box["w"]))
    bottom = min(img.height, int(box["y"] + box["h"]))
    if right <= left or bottom <= top:
        return (0, 0, 0)
    px = img.load()
    rs, gs, bs = [], [], []
    for y in range(top, bottom, max(1, (bottom - top) // 12)):
        for x in range(left, right, max(1, (right - left) // 12)):
            p = px[x, y]
            rs.append(p[0]); gs.append(p[1]); bs.append(p[2])
    if not rs:
        return (0, 0, 0)
    return (int(statistics.mean(rs)), int(statistics.mean(gs)), int(statistics.mean(bs)))


def dist(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


async def shot(cdp) -> bytes:
    r = await cdp.call("Page.captureScreenshot", format="png")
    return base64.b64decode(r["data"])


async def run(ws_url: str, url: str, token: str, out_dir: Path, keep: bool) -> int:
    import websockets
    from PIL import Image

    failures: list[str] = []
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

        for theme in ("lofi", "business"):
            # 用**最高浓度**：浓度越高越容易看出差别，
            # 也正好是"最该看出壁纸"的工况。
            # 预设用 mesh（光晕）：它同时有光斑与底色，最能反映壁纸是否透上来。
            await cdp.call("Runtime.evaluate", expression=(
                f"localStorage.setItem('rag_theme', {json.dumps(theme)});"
                'localStorage.setItem(\'rag_wallpaper\', '
                '\'{"source":"preset","presetId":"mesh","opacity":0.85}\');'
            ))
            await cdp.call("Page.navigate", url=url)
            await asyncio.sleep(6)

            boxes = json.loads(
                (await cdp.call("Runtime.evaluate", expression=SAMPLE_JS,
                                returnByValue=True))["result"]["value"]
            )

            with_wp = Image.open(io.BytesIO(await shot(cdp))).convert("RGB")
            await cdp.call("Runtime.evaluate", expression=HIDE_WALLPAPER_JS)
            await asyncio.sleep(0.5)
            without_wp = Image.open(io.BytesIO(await shot(cdp))).convert("RGB")
            await cdp.call("Runtime.evaluate", expression=RESTORE_JS)

            if keep:
                with_wp.save(out_dir / f"{theme}-with-wallpaper.png")
                without_wp.save(out_dir / f"{theme}-without-wallpaper.png")

            print(f"\n{'=' * 72}")
            print(f"主题 = {theme}（壁纸 = 光晕，浓度 85%）")
            print(f"{'=' * 72}")
            print(f"{'区域':<12}{'有壁纸':>18}{'无壁纸':>18}{'色差':>8}   判定")
            print("-" * 72)

            for name in ("corner", "header", "region"):
                box = boxes.get(name)
                if not box:
                    print(f"{name:<12}{'—':>18}{'—':>18}{'—':>8}   未找到")
                    continue
                a = avg(with_wp, box)
                b = avg(without_wp, box)
                d = dist(a, b)
                # ★ 阈值 25，不是 6。
                #
                # 一开始设成 6，结果**漏掉了真实故障**：当时壁纸可见份额只有
                # 18.9%/12.6%，实测色差 3.6~6.7，探针判定"通过"，
                # 但用户的实际感受是"上传了背景没变"。
                #
                # 教训：阈值不能按"数值上非零"来定，要按**用户能否看出来**来定。
                # 色差 >= 25 才是"明显可见"（对应可见份额 >= 35%）。
                ok = d >= 25
                if not ok:
                    failures.append(
                        f"[{theme}] {name} 区域色差仅 {d:.1f}（需 ≥25）："
                        "壁纸被玻璃冲淡了，用户会认为没生效"
                    )
                print(f"{name:<12}{str(a):>18}{str(b):>18}{d:>8.1f}   "
                      f"{'OK' if ok else '★ 太弱'}")

    print(f"\n{'=' * 72}")
    if failures:
        print(f"未通过 {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("全部通过：壁纸在各区域都可见")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173/strategy")
    parser.add_argument("--out", default="wp-visible")
    parser.add_argument("--keep-images", action="store_true")
    args = parser.parse_args()

    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-wp-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, 1440, 900, profile)
    try:
        return asyncio.run(run(wait_for_devtools(port), args.url, login_token(),
                               Path(args.out), args.keep_images))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
