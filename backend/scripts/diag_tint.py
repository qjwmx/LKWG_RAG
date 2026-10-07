"""诊断 tint-* 小标签的实际计算样式。

起因：probe_glass 报告 lofi 主题下 `tint-primary` 徽标的底色是
(244,225,235) —— **粉紫色**，而 --color-primary 是靛蓝 oklch(52% 0.16 258)。
靛蓝混白不可能得到粉紫。这个"颜色对不上"说明要么
tint-primary 没生效（有别的规则在赢），要么混色发生在意外的空间里。

对比度只差 0.03（4.47 vs 4.50），属于"看起来几乎没问题"的典型——
正因为如此，必须查清底色到底是谁给的，而不是靠微调数字蒙过去。

用法::

    python -m scripts.diag_tint
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

JS = r"""
(() => {
  const out = [];
  for (const el of document.querySelectorAll('.tint-primary, .tint-neutral, .tint-error')) {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    out.push({
      cls: (el.className || '').toString().slice(0, 80),
      text: (el.textContent || '').trim().slice(0, 12),
      bg: cs.backgroundColor,
      color: cs.color,
      w: Math.round(r.width),
      h: Math.round(r.height),
      // 谁提供了这个底色？把自身与祖先的底色都列出来，便于判断
      // 是不是某个祖先的底色"透"上来造成的错觉。
      parentBg: el.parentElement ? getComputedStyle(el.parentElement).backgroundColor : null,
      parentCls: el.parentElement ? (el.parentElement.className || '').toString().slice(0, 60) : null,
    });
  }
  const root = getComputedStyle(document.documentElement);
  return JSON.stringify({
    theme: document.documentElement.getAttribute('data-theme'),
    primary: root.getPropertyValue('--color-primary').trim(),
    base100: root.getPropertyValue('--color-base-100').trim(),
    baseContent: root.getPropertyValue('--color-base-content').trim(),
    items: out.slice(0, 8),
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


async def run(ws_url: str, url: str, token: str) -> int:
    import websockets

    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024, proxy=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Page.enable")
        await cdp.call("Emulation.setDeviceMetricsOverride",
                       width=1440, height=900, deviceScaleFactor=1, mobile=False)
        await cdp.call("Page.navigate", url="http://127.0.0.1:5173/login")
        await asyncio.sleep(3)
        await cdp.call("Runtime.evaluate",
                       expression=f"localStorage.setItem('rag_token', {json.dumps(token)})")

        for theme in ("lofi", "business"):
            await cdp.call("Runtime.evaluate", expression=(
                f"localStorage.setItem('rag_theme', {json.dumps(theme)});"
                'localStorage.setItem(\'rag_wallpaper\', '
                '\'{"source":"none","presetId":"indigo","opacity":0.35}\');'
            ))
            await cdp.call("Page.navigate", url=url)
            await asyncio.sleep(5)
            data = json.loads(
                (await cdp.call("Runtime.evaluate", expression=JS,
                                returnByValue=True))["result"]["value"]
            )
            print(f"\n{'=' * 70}")
            print(f"主题={data['theme']}  primary={data['primary']}  base-100={data['base100']}")
            print(f"{'=' * 70}")
            for it in data["items"]:
                print(f"  {it['text']!r:<10} {it['cls'][:52]}")
                print(f"      bg={it['bg']}  color={it['color']}  {it['w']}x{it['h']}")
                print(f"      父底色={it['parentBg']}  父cls={it['parentCls']}")
    return 0


def main() -> int:
    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-tint-{os.getpid()}"
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
