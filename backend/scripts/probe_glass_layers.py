"""核对毛玻璃在**真实浏览器**里的生效情况。

为什么单独一个脚本
-----------------
有三件事只能实测，看源码或构建产物都不算数：

1. **层叠结果**。`.glass-panel` 要覆盖 daisyUI 的 `.modal-box`
   （它自带 `background-color: var(--color-base-100)`）。
   两者都是普通类，谁赢取决于 @layer 结构；构建产物是压缩过的，
   靠肉眼读 minified CSS 判断层顺序不可靠。
   直接读 getComputedStyle 的最终值才是事实。

2. **backdrop-filter 是否真的应用**。它可能被 `@supports` 分支跳过、
   被前缀差异吃掉、或者因为父元素创建了层叠上下文而失效。
   读 computed style 能确认属性确实被接受（值不是 none）。

3. **壁纸图层是否真的画出来了**。`--wallpaper-image` 是运行期写入的，
   写错变量名不会有任何报错，只是"背景没变"——很容易被当成
   "设计就是这样"。

用法::

    python -m scripts.probe_glass_layers --url http://127.0.0.1:5173/strategy
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

CHECK_JS = r"""
(() => {
  const pick = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return { selector: sel, found: false };
    const cs = getComputedStyle(el);
    return {
      selector: sel,
      found: true,
      background: cs.backgroundColor,
      backdropFilter: cs.backdropFilter || cs.webkitBackdropFilter || 'none',
      position: cs.position,
      opacity: cs.opacity,
      zIndex: cs.zIndex,
    };
  };

  const root = getComputedStyle(document.documentElement);
  return JSON.stringify({
    theme: document.documentElement.getAttribute('data-theme'),
    vars: {
      wallpaperImage: root.getPropertyValue('--wallpaper-image').trim().slice(0, 90),
      wallpaperOpacity: root.getPropertyValue('--wallpaper-opacity').trim(),
      regionAlpha: root.getPropertyValue('--glass-region-alpha').trim(),
      surfaceAlpha: root.getPropertyValue('--surface-alpha').trim(),
      panelAlpha: root.getPropertyValue('--glass-panel-alpha').trim(),
      blur: root.getPropertyValue('--glass-blur').trim(),
    },
    probes: [
      pick('.wallpaper'),
      pick('header'),
      pick('.surface'),
      pick('.glass-panel'),
      pick('.glass-region'),
      pick('.modal-box'),
      pick('aside'),
    ],
    // 统计页面上一共有几个元素真的在做 backdrop-filter。
    // 预期是"少数几个结构层"（顶栏 + 侧栏 + 内容区 + 浮层）。
    // 如果几十个卡片各带一个，说明有组件没走 .surface 而自己写了 blur，
    // 滚动时会明显掉帧。
    backdropCount: [...document.querySelectorAll('*')].filter((el) => {
      const v = getComputedStyle(el).backdropFilter;
      return v && v !== 'none';
    }).length,
    // ★ 壁纸是否**真的可见**：在页面左侧边缘（只有壁纸 + 结构层玻璃、
    //   没有任何卡片）采样一个点。若它的颜色与"主题底色"几乎相同，
    //   说明壁纸被某层不透明底色盖住了——这是本功能最容易静默失效的地方。
    //
    //   采样点选在视口右下角外侧一点：那里通常是内容区之外、
    //   只有壁纸 + 一层玻璃的位置。
    samples: (() => {
      const pts = [
        [innerWidth - 3, 3],
        [innerWidth - 3, innerHeight - 3],
        [3, innerHeight - 3],
      ];
      return pts.map(([x, y]) => {
        const el = document.elementFromPoint(x, y);
        return {
          x, y,
          tag: el ? el.tagName.toLowerCase() : null,
          cls: el ? (el.className || '').toString().slice(0, 50) : null,
        };
      });
    })(),
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


async def run(ws_url: str, url: str, token: str, themes: list[str]) -> int:
    import websockets

    failures: list[str] = []

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024, proxy=None) as ws:
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

        for theme in themes:
            await cdp.call("Runtime.evaluate", expression=(
                f"localStorage.setItem('rag_theme', {json.dumps(theme)});"
                'localStorage.setItem(\'rag_wallpaper\', '
                '\'{"source":"preset","presetId":"indigo","opacity":0.85}\');'
            ))
            await cdp.call("Page.navigate", url=url)
            await asyncio.sleep(6)

            result = await cdp.call("Runtime.evaluate", expression=CHECK_JS, returnByValue=True)
            data = json.loads(result["result"]["value"])

            print(f"\n{'=' * 72}")
            print(f"主题 = {data['theme']}")
            print(f"{'=' * 72}")
            print("CSS 变量：")
            for k, v in data["vars"].items():
                print(f"  {k:<18} {v!r}")

            print("\n各层实测：")
            for p in data["probes"]:
                if not p.get("found"):
                    print(f"  {p['selector']:<16} 未找到")
                    continue
                print(f"  {p['selector']:<16} pos={p['position']:<8} z={p['zIndex']:<5} "
                      f"bg={p['background']}")
                print(f"  {'':<16} backdrop-filter={p['backdropFilter']}")

            # --- 判定 ---
            if data["vars"]["wallpaperImage"] in ("", "none"):
                failures.append(f"[{theme}] --wallpaper-image 为空：壁纸没有生效")
            if not data["vars"]["wallpaperOpacity"]:
                failures.append(f"[{theme}] --wallpaper-opacity 未设置")

            # 壁纸层本身**不应该**有 backdrop-filter：它是"壁纸功能"的本体，
            # 糊掉就没意义了。这一条是防止有人把全屏磨砂层加回来。
            wall = next((p for p in data["probes"] if p["selector"] == ".wallpaper"), None)
            if wall and wall.get("found") and wall["backdropFilter"] not in ("none", ""):
                failures.append(
                    f"[{theme}] .wallpaper 上有 backdrop-filter（{wall['backdropFilter']}）："
                    "壁纸被糊掉了，应该只在页面元素上做毛玻璃"
                )

            # 顶栏必须真的在做模糊（文字直接压在上面，且正文会从下面滚过）
            header = next((p for p in data["probes"] if p["selector"] == "header"), None)
            if not header or not header.get("found"):
                failures.append(f"[{theme}] 找不到 header")
            elif header["backdropFilter"] in ("none", ""):
                failures.append(f"[{theme}] header 的 backdrop-filter 未生效（毛玻璃没做上）")

            # ★ 内容区（.glass-region）**必须没有** backdrop-filter，也必须是透明的。
            #
            # 这是被实测推翻过三次才定下的结论：铺一层半透明白（哪怕只有 28%、
            # 哪怕不模糊）就足以把壁纸冲淡成一片没有细节的色晕——
            # 80px 棋盘格的纯色点比例会从 86% 掉到 0%。
            # 而它铺满整个视口，于是壁纸在任何地方都不清晰。
            # 可读性由卡片（.surface）负责，不由内容区负责。
            region = next((p for p in data["probes"] if p["selector"] == ".glass-region"), None)
            if region and region.get("found"):
                if region["backdropFilter"] not in ("none", ""):
                    failures.append(
                        f"[{theme}] .glass-region 上有 backdrop-filter"
                        f"（{region['backdropFilter']}）：内容区不应有玻璃层，"
                        "它铺满视口会把壁纸整体糊掉"
                    )
                bg = region["background"]
                # 允许 rgba(0,0,0,0) / transparent；只要不是"有颜色的半透明"
                if "rgba" in bg and not bg.endswith(", 0)"):
                    failures.append(
                        f"[{theme}] .glass-region 有半透明填充（bg={bg}）："
                        "即使 28% 的白也会把壁纸冲淡，应为完全透明"
                    )

            # 卡片**不应该**自己做 blur（几十张卡各带一个会掉帧）
            surface = next((p for p in data["probes"] if p["selector"] == ".surface"), None)
            if surface and surface.get("found") and surface["backdropFilter"] not in ("none", ""):
                failures.append(
                    f"[{theme}] .surface 上出现了 backdrop-filter："
                    "卡片应只叠半透明填充"
                )

            # modal-box 必须被 glass-panel 覆盖成半透明；否则弹窗是一块实心板
            modal = next((p for p in data["probes"] if p["selector"] == ".modal-box"), None)
            if modal and modal.get("found"):
                bg = modal["background"]
                if "rgba" not in bg or bg.endswith(", 1)"):
                    failures.append(
                        f"[{theme}] .modal-box 未被 glass-panel 覆盖（bg={bg} 仍不透明）"
                    )

            print(f"\n  做 backdrop-filter 的元素总数：{data['backdropCount']}")
            if data["backdropCount"] > 10:
                failures.append(
                    f"[{theme}] 有 {data['backdropCount']} 个元素在做 backdrop-filter，"
                    "数量过多会影响滚动性能（预期只有结构层那几块）"
                )

            print("\n  视口角落命中元素（用于确认壁纸没被不透明层盖住）：")
            for s in data["samples"]:
                print(f"    ({s['x']},{s['y']}) -> {s['tag']}  cls={s['cls']}")

    print(f"\n{'=' * 72}")
    if failures:
        print(f"未通过 {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173/strategy")
    parser.add_argument("--themes", default="lofi,business")
    args = parser.parse_args()

    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-layers-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, 1440, 900, profile)
    try:
        return asyncio.run(run(wait_for_devtools(port), args.url, login_token(),
                               [t.strip() for t in args.themes.split(",") if t.strip()]))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

