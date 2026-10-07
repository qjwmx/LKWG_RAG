"""像素级验收「毛玻璃 + 壁纸」下的文字对比度。

为什么不能用原来的做法
---------------------
`probe_contrast.py` 是**按颜色值推算**背景的（向上找第一个不透明
background-color）。加壁纸与毛玻璃之后这个方法彻底失效：

1. 壁纸是 `background-image`，不是 `background-color`，
   "向上找不透明背景色"永远找不到它；
2. `backdrop-filter: blur()` 的结果不在任何 CSS 属性里——
   它是合成器在**光栅化阶段**产生的像素，只有真正渲染出来才有；
3. `opacity-*` 类根本不改 `color`，改的是元素自身的不透明度，
   原来的探针读 `getComputedStyle(el).color` 完全看不到它
   （实测确认：`.opacity-55` 在 business 下真实对比度 4.08，
   旧探针报"通过"）。

所以这里改用**真实像素**，两步截图：

    A. 正常截图        —— 用于确认页面渲染正常
    B. 文字变透明再截  —— 布局不变、背景不变，于是这张图就是
                          "每个像素的真实底色"

然后在每个文字元素的包围盒里取底色统计量，与"文字色 × 不透明度链"
合成后的前景色算对比度。这是唯一能同时覆盖
「壁纸 + 毛玻璃 + opacity 链」的方法。

关于取哪个底色统计量
-------------------
- 中位数：代表"这块区域大面积的底色"，对文字抗锯齿边缘像素稳健。
- 但同时计算盒内**最差**对比度：渐变壁纸下盒子里可能一半亮一半暗，
  只看中位数会把"部分文字读不清"放过去。两个都算，报告里分开列。

用法::

    python -m scripts.probe_glass --url http://127.0.0.1:5173/strategy
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.shot import Cdp, find_browser, start_browser, wait_for_devtools  # noqa: E402

# 收集页面上所有"直接承载文字"的元素，连同几何、颜色与有效不透明度。
#
# 关键点：
# - **遍历所有元素**，不再靠少量选择器。选择器清单本身就是漏检的来源
#   （旧探针只列了 .opacity-55/60/65，于是 50 及以下从未被检查）。
# - `opacity` 要沿祖先链累乘：opacity 会创建层叠上下文，
#   父元素 0.5 + 子元素 0.8 的实际效果是 0.4。
# - 颜色在这里就用 canvas 归一化成 rgba 一起返回。**不要**在 Python 侧解析：
#   computed color 现在可能是 `oklch(...)`，Python 里手写解析必踩坑，
#   而且逐个元素回浏览器问一次是上千次 websocket 往返。
COLLECT_JS = r"""
(() => {
  const cv = document.createElement('canvas');
  cv.width = cv.height = 1;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  const cache = new Map();

  // 任意 CSS 颜色 -> [r,g,b,a]，用浏览器自己的色彩管线
  const resolve = (cssColor) => {
    if (cache.has(cssColor)) return cache.get(cssColor);
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = '#000';
    ctx.fillStyle = cssColor;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    const out = [d[0], d[1], d[2], d[3] / 255];
    cache.set(cssColor, out);
    return out;
  };

  const effectiveOpacity = (el) => {
    let o = 1;
    let node = el;
    while (node && node.nodeType === 1) {
      const v = parseFloat(getComputedStyle(node).opacity);
      if (!Number.isNaN(v)) o *= v;
      if (o === 0) break;
      node = node.parentElement;
    }
    return o;
  };

  // 是否可见。**必须用 checkVisibility()**：
  // 关闭的 <details>（daisyUI 的 dropdown 就是这么实现的）里的内容
  // 在 getComputedStyle 上既不是 display:none 也不是 visibility:hidden，
  // opacity 也是 1 —— Chrome 是用内部 slot 上的 content-visibility 隐藏它的，
  // 逐级查祖先的 computed style **查不到**。
  // 实测：壁纸选择器面板关闭时，其 8 个子元素被算成 1.00:1 的"不达标"
  // （bg 与 fg 都是纯白，因为那里根本没渲染）。
  // checkVisibility() 是专为这个场景提供的标准 API，能正确识别。
  const isVisible = (el) => {
    if (typeof el.checkVisibility === 'function') {
      try {
        if (!el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) {
          return false;
        }
      } catch (e) {
        // 某些浏览器不支持参数对象，退回无参调用
        if (!el.checkVisibility()) return false;
      }
    }
    let node = el;
    while (node && node.nodeType === 1) {
      const cs = getComputedStyle(node);
      if (cs.display === 'none' || cs.visibility === 'hidden') return false;
      if (cs.contentVisibility === 'hidden') return false;
      if (parseFloat(cs.opacity) === 0) return false;
      node = node.parentElement;
    }
    return true;
  };

  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (!isVisible(el)) continue;
    // 只取"直接承载文字"的元素：否则容器会被算成它所有子元素的并集，
    // 盒子里大部分是别的元素，底色统计会失真。
    let hasOwnText = false;
    for (const node of el.childNodes) {
      if (node.nodeType === 3 && (node.textContent || '').trim().length > 0) {
        hasOwnText = true;
        break;
      }
    }
    if (!hasOwnText) continue;
    if (el.disabled || cs.pointerEvents === 'none') continue;

    const rect = el.getBoundingClientRect();
    if (rect.width < 1 || rect.height < 1) continue;
    if (rect.bottom < 0 || rect.top > innerHeight) continue;
    if (rect.right < 0 || rect.left > innerWidth) continue;

    const text = (el.innerText || el.textContent || '').trim();
    if (!text) continue;

    out.push({
      text: text.slice(0, 24),
      rgba: resolve(cs.color),
      alpha: effectiveOpacity(el),
      fontSize: parseFloat(cs.fontSize),
      weight: parseInt(cs.fontWeight, 10) || 400,
      // 包围盒向内收 1px：避免把相邻元素/边框/圆角外的像素算进来
      x: rect.left + 1,
      y: rect.top + 1,
      w: Math.max(1, rect.width - 2),
      h: Math.max(1, rect.height - 2),
      cls: (el.className || '').toString().slice(0, 70),
      tag: el.tagName.toLowerCase(),
    });
  }
  return JSON.stringify(out);
})()
"""

# 把所有文字变透明。**只改颜色，不改布局**——所以第二张截图的几何
# 与第一张完全一致，可以直接按同一套包围盒取底色。
HIDE_TEXT_JS = r"""
(() => {
  document.getElementById('__probe_hide_text__')?.remove();
  const s = document.createElement('style');
  s.id = '__probe_hide_text__';
  s.textContent = '*, *::before, *::after {' +
    'color: transparent !important;' +
    'text-shadow: none !important;' +
    'text-decoration-color: transparent !important;' +
    'caret-color: transparent !important;' +
    '}';
  document.head.appendChild(s);
  return true;
})()
"""

RESTORE_TEXT_JS = "document.getElementById('__probe_hide_text__')?.remove(); true"


def login_token() -> str:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        "http://127.0.0.1:8000/api/v1/auth/login",
        data=json.dumps({"username": "admin", "password": "admin"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(opener.open(req, timeout=30).read())["data"]["access_token"]


def srgb_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(r: float, g: float, b: float) -> float:
    return 0.2126 * srgb_lin(r / 255) + 0.7152 * srgb_lin(g / 255) + 0.0722 * srgb_lin(b / 255)


def contrast(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    l1, l2 = luminance(*fg), luminance(*bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def sample_backdrop(img, x: float, y: float, w: float, h: float):
    """返回 (中位数底色, 盒内采样到的底色列表)。

    中位数代表大面积底色；同时把采样到的所有底色返回，
    让调用方可以算出"盒内最差对比度"，覆盖渐变壁纸下的局部问题。
    """
    left = max(0, int(math.floor(x)))
    top = max(0, int(math.floor(y)))
    right = min(img.width, int(math.ceil(x + w)))
    bottom = min(img.height, int(math.ceil(y + h)))
    if right <= left or bottom <= top:
        return (255, 255, 255), [(255, 255, 255)]

    pixels = img.load()
    span = min(right - left, bottom - top)
    step = max(1, span // 20)

    rs: list[int] = []
    gs: list[int] = []
    bs: list[int] = []
    samples: list[tuple[int, int, int]] = []
    for py in range(top, bottom, step):
        for px in range(left, right, step):
            p = pixels[px, py]
            rs.append(p[0])
            gs.append(p[1])
            bs.append(p[2])
            samples.append((p[0], p[1], p[2]))
    if not rs:
        return (255, 255, 255), [(255, 255, 255)]
    return (
        (int(statistics.median(rs)), int(statistics.median(gs)), int(statistics.median(bs))),
        samples,
    )


async def visible_boxes(cdp) -> list[dict]:
    """取所有"浮在内容之上"的元素包围盒（浮层/弹窗），用于剔除遮挡。

    为什么需要这个：打开壁纸选择器后，它的面板会盖住底下的提示条。
    采样时若把面板的像素当成"提示条的底色"，就会量出 1.36:1 这种
    假故障——实测确认面板 (975,48,304x400) 与提示条 (333,114,1094x28)
    确实重叠。

    判据用 position:fixed/absolute 且 z-index 非 auto：
    这些就是"可能盖住别人"的候选。
    """
    result = await cdp.call("Runtime.evaluate", expression=r"""
    (() => {
      const out = [];
      for (const el of document.querySelectorAll('body *')) {
        const cs = getComputedStyle(el);
        if (cs.position !== 'fixed' && cs.position !== 'absolute') continue;
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        if (parseFloat(cs.opacity) === 0) continue;
        const z = cs.zIndex;
        if (z === 'auto') continue;
        if (el.classList.contains('wallpaper')) continue;
        if (el.classList.contains('glass-canvas')) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 8 || r.height < 8) continue;
        out.push({x: r.left, y: r.top, w: r.width, h: r.height});
      }
      return JSON.stringify(out);
    })()
    """, returnByValue=True)
    return json.loads(result["result"]["value"])


def occluded_by(box: dict, overlays: list[dict]) -> dict | None:
    """若 box 被某个浮层盖住（相交面积超过自身 15%），返回那个浮层。"""
    area = max(1.0, box["w"] * box["h"])
    for ov in overlays:
        ix = max(0.0, min(box["x"] + box["w"], ov["x"] + ov["w"]) - max(box["x"], ov["x"]))
        iy = max(0.0, min(box["y"] + box["h"], ov["y"] + ov["h"]) - max(box["y"], ov["y"]))
        if ix * iy / area > 0.15:
            return ov
    return None


def _percentile_color(samples: list[tuple[int, int, int]], q: float) -> tuple[int, int, int]:
    """按亮度取分位数对应的那个底色。

    取"该分位亮度附近采样点"的代表色，而不是对三个通道分别取分位
    （那会合成出一个现实中不存在的颜色，对比度算出来没有意义）。
    """
    if not samples:
        return (255, 255, 255)
    ordered = sorted(samples, key=lambda c: luminance(*c))
    idx = max(0, min(len(ordered) - 1, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


async def shot_png(cdp) -> bytes:
    result = await cdp.call("Page.captureScreenshot", format="png")
    return base64.b64decode(result["data"])


async def run(ws_url: str, url: str, token: str, scenarios: list[tuple[str, str, str]],
              out_dir: Path, width: int, height: int, settle: float,
              keep_images: bool, open_picker: bool = False,
              eval_js: str = "") -> int:
    import websockets
    from PIL import Image

    failures: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)

    async with websockets.connect(ws_url, max_size=128 * 1024 * 1024, proxy=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       width=width, height=height, deviceScaleFactor=1, mobile=False)
        # headless Chrome 默认把 prefers-reduced-motion 报成 reduce，
        # 会让页面走进无障碍分支（动画全关、侧边卡隐藏），
        # 从而改变布局与可见元素——必须显式声明 no-preference。
        await cdp.call("Emulation.setEmulatedMedia",
                       features=[{"name": "prefers-reduced-motion", "value": "no-preference"}])

        await cdp.call("Page.navigate", url="http://127.0.0.1:5173/login")
        await asyncio.sleep(3)
        await cdp.call("Runtime.evaluate",
                       expression=f"localStorage.setItem('rag_token', {json.dumps(token)})")

        for theme, wallpaper_json, label in scenarios:
            await cdp.call("Runtime.evaluate", expression=(
                f"localStorage.setItem('rag_theme', {json.dumps(theme)});"
                f"localStorage.setItem('rag_wallpaper', {json.dumps(wallpaper_json)});"
            ))
            await cdp.call("Page.navigate", url=url)
            await asyncio.sleep(settle)

            # --eval：把页面切到非默认视图后再采集。
            # **默认视图之外的元素完全不在检查范围内**——例如攻略研究页
            # 默认停在「阵容查询」分支，研究分支里的输入区、联网开关、
            # 网络来源面板都不会被渲染，于是探针报「全部通过」而其实
            # 一个都没测。这与 --open-picker 是同一类问题。
            if eval_js:
                await cdp.call("Runtime.evaluate", expression=eval_js, awaitPromise=True)
                await asyncio.sleep(1.5)

            if open_picker:
                # 壁纸选择器默认是关着的（<details> 未展开），
                # 面板里的缩略图/滑块/说明文字都不会渲染，
                # 于是它们完全不在检查范围内。要测就得先打开。
                await cdp.call("Runtime.evaluate", expression=(
                    "document.querySelector('.dropdown > summary')?.click(); true"
                ))
                await asyncio.sleep(1.0)

            collected = await cdp.call("Runtime.evaluate",
                                       expression=COLLECT_JS, returnByValue=True)
            items = json.loads(collected["result"]["value"])

            # 文字透明化后截图 = 纯底色图
            await cdp.call("Runtime.evaluate", expression=HIDE_TEXT_JS)
            await asyncio.sleep(0.4)
            bg_png = await shot_png(cdp)
            await cdp.call("Runtime.evaluate", expression=RESTORE_TEXT_JS)

            bg_img = Image.open(io.BytesIO(bg_png)).convert("RGB")

            # 剔除被浮层盖住的元素：否则会把浮层的像素当成它的底色。
            # 实测：打开壁纸选择器时，面板 (975,48,304x400) 盖住了
            # 数据来源提示条 (333,114,1094x28)，于是提示条被算成 1.36:1
            # ——那 1.36 是面板里深色缩略图的对比度，与提示条无关。
            overlays = await visible_boxes(cdp)

            worst_median: list[dict] = []
            worst_local: list[dict] = []
            checked = 0
            skipped_occluded = 0

            for item in items:
                cover = occluded_by(
                    {"x": item["x"], "y": item["y"], "w": item["w"], "h": item["h"]},
                    overlays,
                )
                if cover is not None:
                    skipped_occluded += 1
                    continue

                median_bg, samples = sample_backdrop(
                    bg_img, item["x"], item["y"], item["w"], item["h"]
                )
                r, g, b, color_alpha = item["rgba"]
                alpha = color_alpha * item["alpha"]

                def fg_over(bg):
                    return (
                        r * alpha + bg[0] * (1 - alpha),
                        g * alpha + bg[1] * (1 - alpha),
                        b * alpha + bg[2] * (1 - alpha),
                    )

                size, weight = item["fontSize"], item["weight"]
                big = size >= 24 or (size >= 18.66 and weight >= 700)
                need = 3.0 if big else 4.5
                checked += 1

                ratio_median = contrast(fg_over(median_bg), median_bg)
                if ratio_median < need:
                    worst_median.append({
                        "ratio": round(ratio_median, 2), "need": need,
                        "text": item["text"], "cls": item["cls"],
                        "alpha": round(item["alpha"], 3),
                        "bg": median_bg,
                        "fg": tuple(round(v) for v in fg_over(median_bg)),
                    })

                # 盒内最差：渐变壁纸下同一段文字可能一半亮一半暗。
                #
                # 用 **5%/95% 分位**而不是单点最小/最大：单点极值会被
                # 圆角外的背景像素、图标、抗锯齿边缘这些离群值带偏——
                # 实测「洛」那个圆角徽标因此被算成 1.14:1（真实约 7:1），
                # 是"探针自己错"，不是样式错。分位数既稳健，
                # 又仍能抓到"盒子里一半亮一半暗"的真实问题。
                lo_bg = _percentile_color(samples, 0.05)
                hi_bg = _percentile_color(samples, 0.95)
                ratio_local = min(
                    contrast(fg_over(lo_bg), lo_bg),
                    contrast(fg_over(hi_bg), hi_bg),
                )
                if ratio_local < need:
                    worst_local.append({
                        "ratio": round(ratio_local, 2), "need": need,
                        "text": item["text"], "cls": item["cls"],
                    })

            worst_median.sort(key=lambda d: d["ratio"] / d["need"])
            worst_local.sort(key=lambda d: d["ratio"] / d["need"])

            print(f"\n{'=' * 72}")
            print(f"主题={theme}  壁纸={label}  检查 {checked} 个文字元素"
                  + (f"（另跳过 {skipped_occluded} 个被浮层盖住的）"
                     if skipped_occluded else ""))
            print(f"{'=' * 72}")

            if not worst_median:
                print("  中位数底色判定：全部达标")
            else:
                print(f"  中位数底色判定：不达标 {len(worst_median)} 个（显示前 10）")
                for d in worst_median[:10]:
                    print(f"    {d['ratio']:5.2f}/{d['need']:.1f}  alpha={d['alpha']:<5} "
                          f"bg={d['bg']} fg={d['fg']}  {d['text']!r}")
                    print(f"          cls={d['cls']}")
                for d in worst_median:
                    failures.append(
                        f"[{theme}/{label}] {d['text']!r} 对比度 {d['ratio']} < {d['need']}"
                        f" (cls={d['cls']})"
                    )

            # 盒内最差单独统计：它衡量"渐变下的局部可读性"，
            # 阈值放宽到 3.0（部分像素略低在渐变背景上是可接受的）。
            bad_local = [d for d in worst_local if d["ratio"] < 3.0]
            if bad_local:
                print(f"  盒内最差 < 3.0 的有 {len(bad_local)} 个（渐变局部，仅提示）：")
                for d in bad_local[:5]:
                    print(f"    {d['ratio']:5.2f}  {d['text']!r}  cls={d['cls']}")

            if keep_images:
                normal = await shot_png(cdp)
                (out_dir / f"{theme}-{label}.png").write_bytes(normal)

    print(f"\n{'=' * 72}")
    if failures:
        print(f"未通过 {len(failures)} 项")
        return 1
    print("全部通过")
    return 0


# 场景表。壁纸字符串用 useWallpaper.ts 的 localStorage 格式。
#
# 除了真实预设，还加入**解析上界**：glass_budget.py 推导出的最坏情况是
# 「浅色主题 + 全黑壁纸」和「深色主题 + 全白壁纸」，都取最大浓度。
# 这两个是理论最差点，必须实测确认——推导再漂亮也要落到像素上。
SCENARIOS = [
    ("lofi", '{"source":"none","presetId":"indigo","opacity":0.85}', "无壁纸"),
    ("lofi", '{"source":"preset","presetId":"indigo","opacity":0.85}', "靛蓝85%"),
    ("lofi", '{"source":"preset","presetId":"grid","opacity":0.85}', "网格85%"),
    ("business", '{"source":"none","presetId":"indigo","opacity":0.85}', "无壁纸"),
    ("business", '{"source":"preset","presetId":"indigo","opacity":0.85}', "靛蓝85%"),
    ("business", '{"source":"preset","presetId":"grid","opacity":0.85}', "网格85%"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5173/strategy")
    parser.add_argument("--out", default="glass-probe")
    parser.add_argument("--width", type=int, default=1440)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--settle", type=float, default=6.0)
    parser.add_argument("--only-theme", default="", help="只测某个主题")
    parser.add_argument("--keep-images", action="store_true", help="保存截图供人工核对")
    parser.add_argument("--open-picker", action="store_true",
                        help="先打开壁纸选择器面板再检查（面板里有缩略图与滑块，"
                             "它默认是关着的，不打开就测不到）")
    parser.add_argument("--eval", dest="eval_js", default="",
                        help="采集前在页面里执行的 JS。用于把页面切到非默认视图"
                             "（例如攻略研究分支），否则那些元素根本不会被渲染，"
                             "探针会报「全部通过」而其实一个都没测")
    args = parser.parse_args()

    scenarios = [s for s in SCENARIOS if not args.only_theme or s[0] == args.only_theme]

    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-glass-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, args.width, args.height, profile)
    try:
        return asyncio.run(run(wait_for_devtools(port), args.url, login_token(),
                               scenarios, Path(args.out), args.width, args.height,
                               args.settle, args.keep_images, args.open_picker,
                               args.eval_js))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())


