"""核对 color-mix 的色相插值，确认 ink-* 与 tint-* 的颜色是否被"绕远路"。

问题
----
daisyUI 的主题变量把中性色写成 `oklch(100% 0 0)` / `oklch(0% 0 0)`——
色相是**显式的 0**，而不是 `none`。CSS 的 `color-mix(in oklch, …)`
对色相走"最短弧"插值；从显式 0 到某个色相 >180 的颜色，最短弧是**负方向**，
于是混合结果会经过品红：

    mix(primary 258° 15%, base-100 0° 85%)
      → 0×0.85 + (−102)×0.15 = −15.3 → 344.7°   （品红，不是靛蓝！）

实测确认：`tint-primary` 的背景渲染成 `oklch(0.928 0.024 344.7)`。

影响范围
--------
任何 `color-mix(in oklch, <有彩色> N%, <主题中性色>)` 都中招：
- 新增的 tint-* / 改成混 base-100 的 attr-chip
- **以及原有的 ink-error/success/warning/info**（它们混的是 base-content）

后者是既存缺陷：`ink-success` 混的 success 色相是 164°，
最短弧 +164°，混合后色相变成 0.45×0 + 0.55×164 ≈ 90°，
也就是"绿色"变成"黄绿色"。对比度检查抓不到这个——它只看明暗，
不看色相，所以这个缺陷一直没被发现。

修法：改用 `in oklab`。oklab 用直角坐标 (a, b) 插值，不涉及色相弧，
因此不会绕路，且仍在线性感知空间里做混合。

用法::

    python -m scripts.diag_color_mix
"""

from __future__ import annotations

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

# 在页面里对比 oklch 与 oklab 两种插值的结果。
# 用 canvas 把结果转成 sRGB 以便直观对比。
JS = r"""
(() => {
  const cv = document.createElement('canvas');
  cv.width = cv.height = 1;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  const toRgb = (c) => {
    ctx.clearRect(0, 0, 1, 1);
    ctx.fillStyle = '#000';
    ctx.fillStyle = c;
    ctx.fillRect(0, 0, 1, 1);
    const d = ctx.getImageData(0, 0, 1, 1).data;
    return `rgb(${d[0]},${d[1]},${d[2]})`;
  };

  const root = getComputedStyle(document.documentElement);
  const v = (n) => root.getPropertyValue(n).trim();

  const cases = [
    ['ink-success',   `color-mix(in oklch, ${v('--color-success')} 55%, ${v('--color-base-content')})`,
                      `color-mix(in oklab, ${v('--color-success')} 55%, ${v('--color-base-content')})`],
    ['ink-warning',   `color-mix(in oklch, ${v('--color-warning')} 55%, ${v('--color-base-content')})`,
                      `color-mix(in oklab, ${v('--color-warning')} 55%, ${v('--color-base-content')})`],
    ['ink-error',     `color-mix(in oklch, ${v('--color-error')} 55%, ${v('--color-base-content')})`,
                      `color-mix(in oklab, ${v('--color-error')} 55%, ${v('--color-base-content')})`],
    ['ink-info',      `color-mix(in oklch, ${v('--color-info')} 55%, ${v('--color-base-content')})`,
                      `color-mix(in oklab, ${v('--color-info')} 55%, ${v('--color-base-content')})`],
    ['tint-primary',  `color-mix(in oklch, ${v('--color-primary')} 15%, ${v('--color-base-100')})`,
                      `color-mix(in oklab, ${v('--color-primary')} 15%, ${v('--color-base-100')})`],
    ['attr-chip底(水)', `color-mix(in oklch, oklch(58% 0.15 248) 16%, ${v('--color-base-100')})`,
                        `color-mix(in oklab, oklch(58% 0.15 248) 16%, ${v('--color-base-100')})`],
  ];

  return JSON.stringify({
    theme: document.documentElement.getAttribute('data-theme'),
    vars: {
      base100: v('--color-base-100'),
      baseContent: v('--color-base-content'),
      primary: v('--color-primary'),
      success: v('--color-success'),
    },
    cases: cases.map(([name, a, b]) => ({
      name,
      oklch: toRgb(a),
      oklab: toRgb(b),
      rawOklch: a.slice(0, 70),
    })),
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


async def run(ws_url: str, token: str) -> int:
    import websockets

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024, proxy=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Page.navigate", url="http://127.0.0.1:5173/login")
        await asyncio.sleep(3)
        await cdp.call("Runtime.evaluate",
                       expression=f"localStorage.setItem('rag_token', {json.dumps(token)})")

        for theme in ("lofi", "business"):
            await cdp.call("Runtime.evaluate",
                           expression=f"localStorage.setItem('rag_theme', {json.dumps(theme)})")
            await cdp.call("Page.navigate", url="http://127.0.0.1:5173/strategy")
            await asyncio.sleep(4)
            data = json.loads(
                (await cdp.call("Runtime.evaluate", expression=JS,
                                returnByValue=True))["result"]["value"]
            )
            print(f"\n{'=' * 74}")
            print(f"主题={data['theme']}  base-100={data['vars']['base100']}")
            print(f"{'=' * 74}")
            print(f"{'样本':<16}{'in oklch':>16}{'in oklab':>16}   色相偏移")
            print("-" * 74)
            for c in data["cases"]:
                same = c["oklch"] == c["oklab"]
                mark = "一致" if same else "★ 不同"
                print(f"{c['name']:<16}{c['oklch']:>16}{c['oklab']:>16}   {mark}")
    return 0


def main() -> int:
    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-cmix-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, 1440, 900, profile)
    try:
        return asyncio.run(run(wait_for_devtools(port), login_token()))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
