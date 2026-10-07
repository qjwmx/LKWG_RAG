"""用 CDP 探针核对真实渲染出来的对比度与属性色区分度。

为什么必须在浏览器里量，而不是信脚本估算
--------------------------------------
脚本（verify_palette.py）算的是"按色板定义应该得到什么"。
浏览器里可能因为以下原因与估算不符：
- `color-mix` 的实际插值空间与近似算法有偏差
- 主题变量没生效（例如主题名写错 → 整页退化成无样式）
- 类名被 Tailwind tree-shake 掉（daisyUI 5 按源码扫描，未使用的类不产出）
- 某个元素被别的规则覆盖了颜色

上一轮的「黑底黑字」正是这一类：脚本认为没问题，实际渲染对比度 1:1。
所以这里**读 getComputedStyle 的真实值**再算对比度。

用法::

    python -m scripts.probe_contrast --url http://127.0.0.1:5173/strategy
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.shot import Cdp, find_browser, start_browser, wait_for_devtools  # noqa: E402

# 在页面里读真实计算样式。返回 JSON 字符串。
#
# 颜色解析用 **canvas**，不用正则：Chrome 的 getComputedStyle 现在会返回
# `oklch(0.8487 0 0)` 这种现代颜色语法，正则只认 `rgb()` 的话会全部解析失败——
# 第一版探针就是这样，把"解析不了"误报成"颜色太接近 d=0"。
# canvas 的 getImageData 走浏览器自己的色彩管线，任何合法 CSS 颜色都能拿到 sRGB 像素，
# 而且天然处理 alpha 合成，比手写转换可靠得多。
PROBE_JS = r"""
(() => {
  const cv = document.createElement('canvas');
  cv.width = cv.height = 1;
  const ctx = cv.getContext('2d', { willReadFrequently: true });

  // 缓存：一次扫描会问上千个元素，重复解析同一个颜色串很浪费。
  // getComputedStyle 返回的是**活对象**，缓存对象本身不会过期。
  const colorCache = new Map();
  const csCache = new WeakMap();
  const CS = (el) => {
    let cs = csCache.get(el);
    if (!cs) { cs = getComputedStyle(el); csCache.set(el, cs); }
    return cs;
  };

  // 任意 CSS 颜色 -> {r,g,b,a}（0-255 / 0-1），用浏览器自己的管线
  const resolve = (cssColor) => {
    const hit = colorCache.get(cssColor);
    if (hit) return hit;
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = '#000';
    ctx.fillStyle = cssColor;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    const out = { r: d[0], g: d[1], b: d[2], a: d[3] / 255 };
    colorCache.set(cssColor, out);
    return out;
  };

  // WCAG 相对亮度
  const lum = ({ r, g, b }) => {
    const f = (c) => {
      const s = c / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };

  const contrast = (fg, bg) => {
    const l1 = lum(fg), l2 = lum(bg);
    const hi = Math.max(l1, l2), lo = Math.min(l1, l2);
    return (hi + 0.05) / (lo + 0.05);
  };

  // 把带 alpha 的前景按 alpha 合成到背景上，再做对比。
  // 不合成的话，半透明文字会被当成实色，对比度算出来偏高。
  const flatten = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a),
    a: 1,
  });

  // 向上找到第一个**不透明**背景色。
  //
  // 关键：必须一直找到 <html> 为止。daisyUI 把主题底色设在 <html> 上，
  // 而 <body> 的背景是**透明的**（rgba(0,0,0,0)）——循环若在 body 处停下，
  // 会把透明当成黑色底，算出"黑底黑字 1:1"这种假故障。
  const effectiveBg = (el) => {
    let node = el;
    while (node) {
      const bg = resolve(getComputedStyle(node).backgroundColor);
      if (bg.a > 0.85) return bg;
      node = node.parentElement;
    }
    // 一路全透明：按白底处理（浏览器默认画布是白的）
    return { r: 255, g: 255, b: 255, a: 1 };
  };

  // 对**所有匹配元素**取最差对比度，而不是只看第一个。
  // 只看第一个会漏掉"某个变体颜色不达标"的情况——顶栏导航就是实例：
  // 选中项是 text-base-content，未选中项是 /70，只看第一个恰好是选中项，
  // 就发现不了未选中项在深色主题下偏低。
  const measure = (label, selector, needNormal = 4.5, needLarge = 3.0) => {
    const els = [...document.querySelectorAll(selector)];
    if (!els.length) return { label, selector, found: false };
    let worst = null;
    for (const el of els) {
      const cs = getComputedStyle(el);
      // 跳过不可见元素（display:none / visibility:hidden / 零尺寸）
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue;
      // 跳过**禁用**控件：禁用态本来就被刻意压低对比度（表示"不可用"），
      // 对它做 WCAG 检查是误报。典型例子是空的发送按钮（仅有图标 + disabled）。
      if (el.disabled || cs.pointerEvents === 'none') continue;
      // 跳过**纯图标**元素：没有可读文本，WCAG 文字对比度不适用。
      // 判据是"去掉空白后没有文字"，而不是"有没有子元素"——
      // 图标按钮里是 <svg>，innerText 为空但 textContent 可能含空白。
      const hasText = (el.innerText || '').trim().length > 0;
      if (!hasText) continue;

      const bg = effectiveBg(el);
      const fg = flatten(resolve(cs.color), bg);
      const size = parseFloat(cs.fontSize);
      const weight = parseInt(cs.fontWeight, 10) || 400;
      // WCAG：大字号 = ≥24px，或 ≥18.66px 且加粗
      const big = size >= 24 || (size >= 18.66 && weight >= 700);
      const need = big ? needLarge : needNormal;
      const ratio = contrast(fg, bg);
      const item = {
        ratio: Math.round(ratio * 100) / 100,
        need,
        text: (el.textContent || '').trim().slice(0, 20),
        fontSize: cs.fontSize,
        cls: (el.className || '').toString().slice(0, 60),
        fg: `rgb(${Math.round(fg.r)},${Math.round(fg.g)},${Math.round(fg.b)})`,
        bg: `rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})`,
      };
      // 记录"相对阈值最差"的那个：用 ratio/need 而不是裸 ratio，
      // 否则大字号元素会永远排最后，掩盖真正的问题。
      if (!worst || item.ratio / item.need < worst.ratio / worst.need) worst = item;
    }
    if (!worst) return { label, selector, found: false };
    return { label, selector, found: true, count: els.length, ...worst };
  };

  // 属性 chip：收集每个属性的**原始 --attr 颜色**与文字对比度。
  //
  // 注意这里取 `--attr` 而不是 `color`：文字色是 `color-mix(attr, base-content)`，
  // 不同属性混完后会互相靠拢（都往同一个 base-content 靠），
  // 用文字色比距离会把"色板可区分"误判成"不可区分"。
  // 要验证的是**色板本身**，所以取变量原值。
  const chips = [...document.querySelectorAll('.attr-chip')].map((el) => {
    const cs = getComputedStyle(el);
    const bg = effectiveBg(el);
    const fg = flatten(resolve(cs.color), bg);
    const attrRaw = cs.getPropertyValue('--attr').trim();
    const attr = attrRaw ? resolve(attrRaw) : null;
    return {
      text: (el.textContent || '').trim(),
      textColor: `rgb(${Math.round(fg.r)}, ${Math.round(fg.g)}, ${Math.round(fg.b)})`,
      attrColor: attr ? `rgb(${Math.round(attr.r)}, ${Math.round(attr.g)}, ${Math.round(attr.b)})` : '',
      ratio: Math.round(contrast(fg, bg) * 100) / 100,
    };
  });

  // 去重：同一个属性出现多次（按 --attr 去重）
  const seen = new Set();
  const uniqueChips = [];
  for (const c of chips) {
    if (seen.has(c.attrColor)) continue;
    seen.add(c.attrColor);
    uniqueChips.push(c);
  }

  return JSON.stringify({
    theme: document.documentElement.getAttribute('data-theme'),
    probes: [
      measure('页面正文', 'body'),
      measure('顶栏导航', 'header a'),
      measure('侧栏标签', 'aside .text-xs'),
      measure('卡片标题', 'article h4'),
      measure('按钮文字', 'button.btn'),
      measure('属性 chip 文字', '.attr-chip'),
      measure('语义色文字（红/绿/黄/蓝）', '.ink-error, .ink-success, .ink-warning, .ink-info'),
      measure('助手消息', '[data-role="assistant"]'),
      measure('用户消息', '[data-role="user"]'),
      measure('引用卡片', 'button[title*="切片"]'),
      measure('次要文字', '.opacity-55, .opacity-60, .opacity-65'),
    ],
    chipCount: chips.length,
    uniqueChipCount: uniqueChips.length,
    chips: uniqueChips,
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


def rgb_distance(a: str, b: str) -> float:
    """加权 RGB 距离，用于判断两个属性色是否真的能区分。"""
    def parse(v: str) -> tuple[float, float, float]:
        m = __import__("re").search(r"rgba?\(([^)]+)\)", v)
        if not m:
            return (0.0, 0.0, 0.0)
        parts = [float(x) for x in m.group(1).split(",")[:3]]
        return (parts[0], parts[1], parts[2])

    r1, g1, b1 = parse(a)
    r2, g2, b2 = parse(b)
    rmean = (r1 + r2) / 2
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return math.sqrt(
        (2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db
    )


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
            # 用真实主题名导航（主题已在 style.css 里启用，不需要注入）
            await cdp.call("Runtime.evaluate",
                           expression=f"localStorage.setItem('rag_theme', {json.dumps(theme)})")
            await cdp.call("Page.navigate", url=url)
            await asyncio.sleep(6)

            result = await cdp.call("Runtime.evaluate",
                                    expression=PROBE_JS, returnByValue=True)
            data = json.loads(result["result"]["value"])

            print(f"\n{'=' * 62}")
            print(f"主题：{data['theme']}")
            print(f"{'=' * 62}")

            print(f"\n{'元素':<16}{'对比度':>8}{'需':>6}   判定")
            print("-" * 52)
            for probe in data["probes"]:
                if not probe.get("found"):
                    print(f"{probe['label']:<16}{'—':>8}{'':>6}   未找到")
                    continue
                ratio = probe["ratio"]
                need = probe["need"]
                ok = ratio >= need
                if not ok:
                    failures.append(
                        f"[{data['theme']}] {probe['label']} 对比度 {ratio} < {need}"
                        f"（{probe.get('count', 1)} 个元素中最差：{probe.get('text', '')}）"
                    )
                mark = "OK" if ok else "不足"
                print(f"{probe['label']:<16}{ratio:>8.2f}{need:>6.1f}   {mark}")
                if not ok:
                    print(f"{'':<16}  fg={probe.get('fg')} bg={probe.get('bg')}")
                    print(f"{'':<16}  cls={probe.get('cls')}")
                    print(f"{'':<16}  text={probe.get('text')!r}")

            # 属性 chip
            print(f"\n属性 chip：共 {data['chipCount']} 个，{data['uniqueChipCount']} 种属性色")
            bad = [c for c in data["chips"] if c["ratio"] is not None and c["ratio"] < 4.5]
            if bad:
                for c in bad:
                    failures.append(f"[{data['theme']}] chip「{c['text']}」对比度 {c['ratio']} < 4.5")
                print(f"  对比度不足 {len(bad)} 个：")
                for c in bad:
                    print(f"    {c['text']:<5} {c['ratio']:.2f}  {c['textColor']}")
            else:
                print("  对比度全部 ≥ 4.5")

            # 两两距离：验证"属性色板真的可区分"（用 --attr 原值比）
            chips = [c for c in data["chips"] if c.get("attrColor")]
            worst = []
            for i in range(len(chips)):
                for j in range(i + 1, len(chips)):
                    worst.append(
                        (rgb_distance(chips[i]["attrColor"], chips[j]["attrColor"]),
                         chips[i]["text"], chips[j]["text"])
                    )
            worst.sort()
            if worst:
                print(f"  最接近的 3 对：")
                for d, a, b in worst[:3]:
                    flag = "  <<< 太接近" if d < 90 else ""
                    if d < 90:
                        failures.append(f"[{data['theme']}] 属性色过近：{a}/{b} d={d:.0f}")
                    print(f"    {a:<5} {b:<5} d={d:6.1f}{flag}")

    print(f"\n{'=' * 62}")
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
    profile = Path(tempfile.gettempdir()) / f"rag-probe-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, 1440, 900, profile)
    try:
        return asyncio.run(
            run(wait_for_devtools(port), args.url, login_token(),
                [t.strip() for t in args.themes.split(",") if t.strip()])
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
