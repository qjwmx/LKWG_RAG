"""诊断「自定义壁纸上传后没有替换成功」。

为什么必须实测而不是读代码
-------------------------
这条链路上有好几个**静默失败**点，每一个都不会报错、界面看起来也"正常"：

1. `input.files` 赋值后 change 事件没触发（DataTransfer 用法不对）
2. `idbPut` 抛错 → 但错误只写进一个 10px 的小字提示，容易看不见
3. `source` 变成 'custom' 了，但 `--wallpaper-image` 没更新（watch 没触发）
4. CSS 变量更新了，但 `blob:` URL 被 **CSP** 拦掉（图片不加载，背景空白）
5. 图片加载了，但在 35% 浓度 + 玻璃下看不出来

前四种是"真没生效"，第五种是"生效了但看不出"。**两者要修的地方完全不同**，
所以探针必须把它们分开报告：既读 CSS 变量（判断逻辑层），
也把 blob URL 单独丢进 `Image()` 试加载（判断是否被 CSP 拦），
还对比像素（判断视觉层）。

用法::

    python -m scripts.diag_wallpaper_upload
"""

from __future__ import annotations

import argparse
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

# 造一张**高对比**测试图：纯玫红底 + 白色竖条。
#
# 为什么不用普通照片：壁纸会被 35% 浓度 + 玻璃压得很淡，
# 低对比的图片即使正确生效也可能"看不出变化"，导致误判成"没生效"。
# 玫红+白条在合成后仍然能明确改变色相与亮度，且白条提供高频细节，
# 顺便可以观察毛玻璃的模糊是否真的作用在它上面。
INJECT_JS = r"""
(() => {
  const c = document.createElement('canvas');
  c.width = 480; c.height = 320;
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#ff0044';
  ctx.fillRect(0, 0, 480, 320);
  ctx.fillStyle = '#ffffff';
  for (let x = 0; x < 480; x += 16) ctx.fillRect(x, 0, 8, 320);

  return new Promise((resolve) => {
    c.toBlob((blob) => {
      const input = document.querySelector('.dropdown-content input[type=file]');
      if (!input) { resolve(JSON.stringify({ error: 'file input 未找到' })); return; }
      const file = new File([blob], 'probe-wallpaper.png', { type: 'image/png' });
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      resolve(JSON.stringify({ ok: true, bytes: blob.size, name: file.name }));
    }, 'image/png');
  });
})()
"""

# 读状态。既看 CSS 变量（逻辑层），也看元素的实际 background-image（渲染层）。
STATE_JS = r"""
(() => {
  const root = document.documentElement;
  const wp = document.querySelector('.wallpaper');
  const label = document.querySelector('.dropdown-content .ink-subtle');
  const err = document.querySelector('.dropdown-content .ink-error');
  return JSON.stringify({
    varImage: getComputedStyle(root).getPropertyValue('--wallpaper-image').trim().slice(0, 100),
    varOpacity: getComputedStyle(root).getPropertyValue('--wallpaper-opacity').trim(),
    elBg: wp ? getComputedStyle(wp).backgroundImage.slice(0, 100) : null,
    elOpacity: wp ? getComputedStyle(wp).opacity : null,
    stored: localStorage.getItem('rag_wallpaper'),
    label: label ? label.textContent.trim() : null,
    errorText: err ? err.textContent.trim() : null,
  });
})()
"""

# 单独验证 blob URL 能不能被当图片加载。
# 若 CSP 拦了 blob:，这里会走到 error —— 而 CSS 变量、localStorage 全都是"正常"的，
# 所以必须单独测这一项，否则会误判成"逻辑没生效"。
BLOB_LOAD_JS = r"""
(async () => {
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue('--wallpaper-image').trim();
  const m = raw.match(/url\(["']?(.+?)["']?\)/);
  if (!m) return JSON.stringify({ skipped: true, reason: '不是 url() 形式', raw: raw.slice(0, 60) });
  const url = m[1];
  const result = await new Promise((resolve) => {
    const img = new Image();
    const timer = setTimeout(() => resolve({ state: 'timeout' }), 4000);
    img.onload = () => { clearTimeout(timer); resolve({ state: 'ok', w: img.naturalWidth, h: img.naturalHeight }); };
    img.onerror = () => { clearTimeout(timer); resolve({ state: 'error' }); };
    img.src = url;
  });
  return JSON.stringify({ ...result, scheme: url.slice(0, 5) });
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


async def wait_for(cdp, selector: str, timeout: float = 20.0) -> bool:
    """等某个元素出现。

    ★ 不要用固定的 `sleep(6)`：Vite 在重新编译（比如刚跑过 build）
    时首次请求会慢很多，固定等待会拿到**尚未挂载的空页面**，
    于是所有断言都失败，看起来像"功能坏了"。
    实测踩过：一次跑出 8 项全红，重跑就全绿——那种"偶发红"
    最浪费时间，因为它会让人去改本来正确的代码。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        ok = (await cdp.call("Runtime.evaluate", expression=(
            f"!!document.querySelector({json.dumps(selector)})"
        ), returnByValue=True))["result"]["value"]
        if ok:
            return True
        await asyncio.sleep(0.4)
    return False


async def shot(cdp) -> bytes:
    r = await cdp.call("Page.captureScreenshot", format="png")
    return base64.b64decode(r["data"])


def avg_region(img, x: int, y: int, w: int, h: int) -> tuple[int, int, int]:
    left, top = max(0, x), max(0, y)
    right, bottom = min(img.width, x + w), min(img.height, y + h)
    if right <= left or bottom <= top:
        return (0, 0, 0)
    px = img.load()
    rs, gs, bs = [], [], []
    for py in range(top, bottom, max(1, (bottom - top) // 20)):
        for pxx in range(left, right, max(1, (right - left) // 20)):
            p = px[pxx, py]
            rs.append(p[0]); gs.append(p[1]); bs.append(p[2])
    if not rs:
        return (0, 0, 0)
    return (sum(rs) // len(rs), sum(gs) // len(gs), sum(bs) // len(bs))


def dist(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


async def run(ws_url: str, token: str, out_dir: Path, keep: bool) -> int:
    import websockets
    from PIL import Image

    out_dir.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []

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

        # 起点：先用**预设壁纸**，这样"替换"才有可观测的对比基准
        await cdp.call("Runtime.evaluate", expression=(
            "localStorage.setItem('rag_theme','lofi');"
            "localStorage.setItem('rag_wallpaper',"
            "'{\"source\":\"preset\",\"presetId\":\"indigo\",\"opacity\":0.75}');"
        ))
        await cdp.call("Page.navigate", url="http://127.0.0.1:5173/strategy")
        # 等真实元素出现，而不是固定 sleep（见 wait_for 的说明）
        if not await wait_for(cdp, ".wallpaper"):
            print("页面未渲染（.wallpaper 一直没出现）——先检查 dev server")
            return 1
        await asyncio.sleep(1.5)

        # 打开壁纸面板（file input 在里面）
        await cdp.call("Runtime.evaluate",
                       expression="document.querySelector('.dropdown > summary')?.click(); true")
        if not await wait_for(cdp, ".dropdown-content input[type=file]"):
            print("壁纸面板没有打开（file input 未出现）")
            return 1
        await asyncio.sleep(0.8)

        before_state = json.loads((await cdp.call(
            "Runtime.evaluate", expression=STATE_JS, returnByValue=True))["result"]["value"])
        before_png = await shot(cdp)
        before = Image.open(io.BytesIO(before_png)).convert("RGB")

        print("=" * 74)
        print("上传前")
        print("=" * 74)
        for k, v in before_state.items():
            print(f"  {k:<12} {v!r}")

        # ---- 注入文件并触发 change ----
        injected = json.loads((await cdp.call(
            "Runtime.evaluate", expression=INJECT_JS,
            awaitPromise=True, returnByValue=True))["result"]["value"])
        print(f"\n注入文件：{injected}")

        await asyncio.sleep(3.0)

        after_state = json.loads((await cdp.call(
            "Runtime.evaluate", expression=STATE_JS, returnByValue=True))["result"]["value"])
        blob = json.loads((await cdp.call(
            "Runtime.evaluate", expression=BLOB_LOAD_JS,
            awaitPromise=True, returnByValue=True))["result"]["value"])
        after_png = await shot(cdp)
        after = Image.open(io.BytesIO(after_png)).convert("RGB")

        print()
        print("=" * 74)
        print("上传后")
        print("=" * 74)
        for k, v in after_state.items():
            print(f"  {k:<12} {v!r}")
        print(f"\nblob URL 单独试加载：{blob}")

        # ---- 逐项判定 ----
        print()
        print("=" * 74)
        print("逐项判定")
        print("=" * 74)

        def check(label: str, ok: bool, detail: str) -> None:
            print(f"  [{'OK' if ok else '失败'}] {label}")
            print(f"         {detail}")
            if not ok:
                problems.append(f"{label}：{detail}")

        check("change 事件已触发、file 已注入",
              bool(injected.get("ok")),
              f"injected={injected}")

        check("source 已切到 custom（localStorage 记下 custom）",
              '"custom"' in (after_state.get("stored") or ""),
              f"stored={after_state.get('stored')}")

        check("面板标签显示自定义文件名",
              "probe-wallpaper.png" in (after_state.get("label") or ""),
              f"label={after_state.get('label')!r}")

        check("没有报错文案",
              not after_state.get("errorText"),
              f"errorText={after_state.get('errorText')!r}")

        img_var = after_state.get("varImage") or ""
        check("--wallpaper-image 已变成 blob URL",
              "blob:" in img_var,
              f"--wallpaper-image={img_var!r}")

        check("元素实际 background-image 也是 blob URL",
              "blob:" in (after_state.get("elBg") or ""),
              f".wallpaper backgroundImage={after_state.get('elBg')!r}")

        check("blob URL 能被当图片加载（未被 CSP 拦）",
              blob.get("state") == "ok",
              f"load={blob}")

        # ---- 像素层：真正换掉了吗 ----
        box = (700, 500, 400, 300)
        a = avg_region(before, *box)
        b = avg_region(after, *box)
        d = dist(a, b)
        check("渲染像素确实变了",
              d >= 6,
              f"区域{box} 上传前={a} 上传后={b} 色差={d:.1f}")

        if keep:
            (out_dir / "upload-before.png").write_bytes(before_png)
            (out_dir / "upload-after.png").write_bytes(after_png)

        # 上传后回到预设，确认还能切回去（排查"卡在 custom 出不来"）
        #
        # ★ 按 title 选，不按下标：网格里现在有「无壁纸 / 自定义 / 8 个预设」，
        # 下标会随 UI 增删而漂移（这次加了自定义缩略图，第 1 个就从
        # 预设变成了自定义，导致断言误报）。title 是稳定的语义标识。
        await cdp.call("Runtime.evaluate", expression=(
            "document.querySelector('.dropdown-content button[title=\"靛蓝\"]')?.click(); true"))
        await asyncio.sleep(1.5)
        back = json.loads((await cdp.call(
            "Runtime.evaluate", expression=STATE_JS, returnByValue=True))["result"]["value"])
        print()
        print("=" * 74)
        print("点回「靛蓝」预设后")
        print("=" * 74)
        for k, v in back.items():
            print(f"  {k:<12} {v!r}")
        check("能从 custom 切回预设",
              "linear-gradient" in (back.get("varImage") or ""),
              f"--wallpaper-image={back.get('varImage')!r}")

    print()
    print("=" * 74)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("上传链路全部正常（若肉眼仍看不出，是浓度/玻璃造成的观感问题）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="_shot")
    parser.add_argument("--keep-images", action="store_true")
    args = parser.parse_args()

    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-upload-{os.getpid()}"
    shutil.rmtree(profile, ignore_errors=True)
    proc, port = start_browser(browser, 1440, 900, profile)
    try:
        return asyncio.run(run(wait_for_devtools(port), login_token(),
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
