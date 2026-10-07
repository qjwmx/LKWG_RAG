"""验证「自定义壁纸上传」在各种文件下的真实行为。

为什么要驱动**真实 UI** 而不是复刻判定逻辑
-----------------------------------------
第一版这个脚本把 `uploadCustom` 的判定在 JS 里抄了一遍。问题很明显：
源码改了之后，脚本还在测那份**抄件**，于是它继续报告旧行为
（改完代码重跑，输出的还是"会被拒"——差点让我以为修复没生效）。

所以现在改成：往真实的 file input 里塞文件、触发 change、
再读界面与 CSS 变量的**最终结果**。测的是真代码，不是抄件。

覆盖三种文件：
  1. 正常 PNG          —— 基线，必须成功
  2. type 为空串的 PNG —— 模拟 MIME 推断失败（拖拽/U 盘很常见），
                          应靠扩展名兜底后成功
  3. 伪装成 .jpg 的垃圾字节 —— 应被拒绝并给出可见的错误提示，
                          且**不能**把状态改成 custom（否则背景空白、无报错）

用法::

    python -m scripts.diag_wallpaper_cases
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.shot import Cdp, find_browser, start_browser, wait_for_devtools  # noqa: E402

# 造文件并塞进真实的 file input。
# `kind` 决定造哪种文件；MIME 与扩展名都按用例指定。
#
# ★ 必须是**立即调用的**函数表达式：写成 `async (kind) => {...}` 只是
# 一个函数对象，Runtime.evaluate 求值后得到 `{}`，什么都不会发生，
# 而错误还很隐蔽（返回 `{}`、没有异常）。踩过两次，所以用 %s 注入参数。
INJECT_JS = r"""
(async () => {
  const kind = %s;
  const pngBytes = await new Promise((res) => {
    const c = document.createElement('canvas');
    c.width = 40; c.height = 40;
    const x = c.getContext('2d');
    x.fillStyle = '#ff0044'; x.fillRect(0, 0, 40, 40);
    c.toBlob(async (b) => res(new Uint8Array(await b.arrayBuffer())), 'image/png');
  });

  let bytes, name, type;
  if (kind === 'normalPng') {
    bytes = pngBytes; name = 'normal.png'; type = 'image/png';
  } else if (kind === 'emptyMimePng') {
    // 真 PNG，但 MIME 是空串 —— 模拟浏览器推断失败
    bytes = pngBytes; name = 'photo.png'; type = '';
  } else {
    // 伪装成 jpg 的随机字节：MIME 合法但浏览器解不开
    const junk = new Uint8Array(2048);
    for (let i = 0; i < junk.length; i++) junk[i] = (i * 37) %% 256;
    bytes = junk; name = 'broken.jpg'; type = 'image/jpeg';
  }

  const input = document.querySelector('.dropdown-content input[type=file]');
  if (!input) return JSON.stringify({ error: 'file input 未找到' });
  const file = new File([bytes], name, { type });
  const dt = new DataTransfer();
  dt.items.add(file);
  input.files = dt.files;
  input.dispatchEvent(new Event('change', { bubbles: true }));
  return JSON.stringify({ ok: true, name, type, size: file.size });
})()
"""

# 读界面与变量的**最终状态**——不关心内部怎么判的，只看结果。
STATE_JS = r"""
(() => {
  const root = document.documentElement;
  const label = document.querySelector('.dropdown-content .ink-subtle');
  const err = document.querySelector('.dropdown-content .tint-error');
  const wp = document.querySelector('.wallpaper');
  return JSON.stringify({
    stored: localStorage.getItem('rag_wallpaper'),
    varImage: getComputedStyle(root).getPropertyValue('--wallpaper-image').trim(),
    elBg: wp ? getComputedStyle(wp).backgroundImage.slice(0, 60) : null,
    errorText: err ? err.textContent.trim() : null,
    customThumb: !!document.querySelector('.dropdown-content img[alt="自定义壁纸"]'),
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


async def get_state(cdp) -> dict:
    raw = (await cdp.call("Runtime.evaluate", expression=STATE_JS,
                          returnByValue=True))["result"]["value"]
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, dict) and set(data.keys()) == {"out"}:
        data = data["out"]
    return data


async def inject(cdp, kind: str) -> dict:
    raw = (await cdp.call("Runtime.evaluate", expression=INJECT_JS % json.dumps(kind),
                          awaitPromise=True, returnByValue=True))["result"]["value"]
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, dict) and set(data.keys()) == {"out"}:
        data = data["out"]
    return data


async def run(ws_url: str, token: str) -> int:
    import websockets

    problems: list[str] = []

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

        # 每个用例都从"预设壁纸"这个干净起点开始，
        # 否则上一个用例留下的 custom 状态会污染判定。
        async def reset() -> None:
            await cdp.call("Runtime.evaluate", expression=(
                "localStorage.setItem('rag_theme','lofi');"
                "localStorage.setItem('rag_wallpaper',"
                "'{\"source\":\"preset\",\"presetId\":\"indigo\",\"opacity\":0.45}');"
            ))
            await cdp.call("Page.navigate", url="http://127.0.0.1:5173/strategy")
            await asyncio.sleep(5)
            await cdp.call("Runtime.evaluate", expression=(
                "document.querySelector('.dropdown > summary')?.click(); true"))
            await asyncio.sleep(1.0)

        cases = [
            # kind, 期望成功?, 说明
            ("normalPng", True, "正常 PNG"),
            ("emptyMimePng", True, "type 为空串的合法 PNG（应靠扩展名兜底）"),
            ("brokenJpg", False, "伪装成 .jpg 的损坏文件（应被拒绝）"),
        ]

        for kind, should_succeed, desc in cases:
            await reset()
            injected = await inject(cdp, kind)
            await asyncio.sleep(2.5)
            st = await get_state(cdp)

            succeeded = "custom" in (st.get("stored") or "")
            print(f"\n{'=' * 76}")
            print(f"{desc}")
            print(f"{'=' * 76}")
            print(f"  注入：{injected}")
            print(f"  localStorage : {st.get('stored')}")
            print(f"  --wallpaper  : {st.get('varImage', '')[:64]}")
            print(f"  自定义缩略图 : {st.get('customThumb')}")
            print(f"  错误提示     : {st.get('errorText')!r}")
            print(f"  → 结果：{'成功（切到 custom）' if succeeded else '被拒绝（保持原壁纸）'}")

            if should_succeed and not succeeded:
                problems.append(
                    f"{desc}：应当成功但被拒绝"
                    f"（错误提示={st.get('errorText')!r}）"
                )
            if not should_succeed:
                if succeeded:
                    problems.append(
                        f"{desc}：应当被拒绝，但状态变成了 custom"
                        "——背景会空白且没有任何报错"
                    )
                elif not st.get("errorText"):
                    problems.append(
                        f"{desc}：被拒绝了但**没有错误提示**，用户不知道发生了什么"
                    )
                # 被拒绝时必须**保持原来的壁纸**，不能变成空白
                if "linear-gradient" not in (st.get("varImage") or ""):
                    problems.append(f"{desc}：被拒绝后壁纸状态被破坏（{st.get('varImage')!r}）")

            if should_succeed and st.get("errorText"):
                problems.append(f"{desc}：成功了却仍显示错误提示 {st.get('errorText')!r}")

    print()
    print("=" * 76)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("三种文件的行为都正确")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="_shot")
    args = parser.parse_args()

    browser = find_browser()
    profile = Path(tempfile.gettempdir()) / f"rag-cases-{os.getpid()}"
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
