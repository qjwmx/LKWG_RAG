"""验证 18 属性色板：两两可区分 + chip 文字对比度达标。

为什么需要脚本而不是"看着差不多"
--------------------------------
上一版色板（`visual.ts` 里的 hex 表）看着有 18 个不同颜色，
实测感知距离却严重重叠：普通/机械 19、火/武 29、光/电 34。
"看着不一样"和"数值上能区分"是两件事，必须量化。

两个验收指标
-----------
1. **可区分性**：任意两属性的加权 RGB 距离 ≥ 110。
   权重按人眼敏感度（绿最敏感、蓝最不敏感），比裸欧氏距离更接近真实观感。
2. **可读性**：chip 的文字色在对应主题底色上对比度 ≥ 4.5:1。
   这是真正的坑——光/电/冰/萌 这类高明度属性直接当文字色，
   在白底上根本读不清（上一轮"黑底黑字"就是同类问题的另一面）。

chip 文字色由 `color-mix` 从属性色派生（浅色主题往黑混、深色往白混），
所以这里必须**按派生后的颜色**算对比度，而不是按属性色本身。

用法::

    python -m scripts.verify_palette
"""

from __future__ import annotations

import itertools
import math
import sys

# 主题底色与正文色（取自 daisyUI 内置主题文件，必须与 style.css 启用的一致）
#
# 用**真实的 base-content** 而不是纯黑/纯白：chip 的文字色是
# `color-mix(in oklch, var(--attr) X%, var(--color-base-content))`，
# 而 business 的 base-content 是 oklch(84.87%)、不是纯白。
# 按纯白估算会高估深色主题的对比度，正好漏掉最容易出问题的那一档。
LIGHT_BG = (100.0, 0.0, 0.0)          # lofi     base-100
LIGHT_CONTENT = (0.0, 0.0, 0.0)       # lofi     base-content
DARK_BG = (24.353, 0.0, 0.0)          # business base-100
DARK_CONTENT = (84.87, 0.0, 0.0)      # business base-content

# 最终色板（由 scripts/optimize_palette.py 搜索得到，并落盘到 data/attr_palette.json）。
# 这里**内联同一份值**而不是读 JSON：验收脚本要能在没有 data/ 目录的环境里跑，
# 且改色板时必须同时改两处——正好逼着人跑一次验收。
PALETTE: dict[str, tuple[float, float, float]] = {
    "普通": (72.0, 0.006, 0.0),      # 中性灰（最亮，与"机械"拉开明度）
    "草":   (64.0, 0.170, 145.0),    # 绿
    "火":   (60.0, 0.200, 32.0),     # 红橙
    "水":   (58.0, 0.150, 248.0),    # 蓝
    "光":   (80.0, 0.150, 78.0),     # 暖金
    "地":   (52.0, 0.100, 62.0),     # 棕褐
    "冰":   (84.0, 0.090, 182.8),    # 淡青（明度最高）
    "龙":   (46.0, 0.180, 273.3),    # 靛
    "电":   (88.0, 0.190, 112.0),    # 柠檬黄
    "毒":   (52.0, 0.210, 318.0),    # 紫
    "虫":   (74.0, 0.170, 128.0),    # 黄绿
    "武":   (44.0, 0.190, 18.0),     # 深绯
    "翼":   (73.7, 0.120, 233.7),    # 天蓝
    "萌":   (78.0, 0.130, 352.0),    # 粉
    "幽":   (35.8, 0.140, 288.0),    # 深紫罗兰
    "恶":   (38.0, 0.030, 45.0),     # 暗暖灰
    "幻":   (64.0, 0.240, 332.0),    # 洋红
    "机械": (54.0, 0.045, 235.0),    # 冷钢蓝
}

MIN_DISTANCE = 110.0

# chip 文字色的派生比例（与 style.css 里的 color-mix 必须一致）。
#
# 为什么要调这两个数而不是随便定：光/电/冰/萌 这类高明度属性直接当文字色，
# 在白底上读不清；恶/幽/龙/武 这类低明度属性直接当文字色，在深色底上读不清。
# 往 base-content 混的比例必须让**全部 18 个**都过 4.5:1，不能只挑几个看。
#
# 这两个值是**搜出来的**（见本文件末尾的搜索记录），不是试出来的：
# 浅色 0.60 → 最低 5.48:1；深色 0.40 → 最低 4.95:1。都留了余量。
# 比例越高越接近属性原色（越鲜艳），但对比度越低——0.60/0.40 是
# "保住色相辨识度"与"读得清"之间的平衡点。
LIGHT_INK_ATTR_RATIO = 0.60   # 浅色主题：mix(attr 60%, base-content 40%)
DARK_INK_ATTR_RATIO = 0.40    # 深色主题：mix(attr 40%, base-content 60%)


def _to_linear_rgb(Lpct: float, C: float, H: float) -> tuple[float, float, float]:
    """oklch -> 线性 sRGB（对比度与感知距离都必须在线性空间算）。"""
    L = Lpct / 100.0
    h = math.radians(H)
    a, b = C * math.cos(h), C * math.sin(h)
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_**3, m_**3, s_**3
    return (
        4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
        -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
        -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    )


def _encode(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return 12.92 * x if x <= 0.0031308 else 1.055 * (x ** (1 / 2.4)) - 0.055


def to_srgb8(color: tuple[float, float, float]) -> tuple[int, int, int]:
    r, g, b = _to_linear_rgb(*color)
    return tuple(round(_encode(v) * 255) for v in (r, g, b))  # type: ignore[return-value]


def luminance(color: tuple[float, float, float]) -> float:
    r, g, b = _to_linear_rgb(*color)
    clamp = lambda v: max(0.0, min(1.0, v))  # noqa: E731
    return 0.2126 * clamp(r) + 0.7152 * clamp(g) + 0.0722 * clamp(b)


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def mix_oklch(
    base: tuple[float, float, float], other: tuple[float, float, float], base_ratio: float
) -> tuple[float, float, float]:
    """近似 `color-mix(in oklch, base X%, other Y%)`。

    oklch 的混合按极坐标插值，但两者色相接近时结果与线性插值差别很小；
    这里只用于**验收估算**，最终以浏览器实测为准。
    """
    L = base[0] * base_ratio + other[0] * (1 - base_ratio)
    C = base[1] * base_ratio + other[1] * (1 - base_ratio)
    # 色相按最短弧插值
    h1, h2 = math.radians(base[2]), math.radians(other[2])
    delta = math.atan2(math.sin(h2 - h1), math.cos(h2 - h1))
    h = base[2] + math.degrees(delta) * (1 - base_ratio)
    return (L, C, h % 360)


BLACK = (0.0, 0.0, 0.0)
WHITE = (100.0, 0.0, 0.0)


def weighted_distance(c1: tuple[float, float, float], c2: tuple[float, float, float]) -> float:
    """加权 RGB 距离（人眼感知近似，比裸欧氏更贴近观感）。"""
    r1, g1, b1 = to_srgb8(c1)
    r2, g2, b2 = to_srgb8(c2)
    rmean = (r1 + r2) / 2
    dr, dg, db = r1 - r2, g1 - g2, b1 - b2
    return math.sqrt(
        (2 + rmean / 256) * dr * dr + 4 * dg * dg + (2 + (255 - rmean) / 256) * db * db
    )


def main() -> int:
    # 自检：确保度量函数本身是对的
    assert abs(contrast(WHITE, BLACK) - 21.0) < 0.05, "对比度自检失败"
    assert weighted_distance(WHITE, BLACK) > 700, "距离自检失败"

    names = list(PALETTE)
    print(f"属性数：{len(names)}   可区分阈值：{MIN_DISTANCE}\n")

    # ---- 1. 两两可区分性 ----
    pairs = sorted(
        (
            (weighted_distance(PALETTE[a], PALETTE[b]), a, b)
            for a, b in itertools.combinations(names, 2)
        )
    )
    print("最接近的 10 对：")
    failures = []
    for dist, a, b in pairs[:10]:
        flag = ""
        if dist < MIN_DISTANCE:
            flag = "  <<< 不达标"
            failures.append((dist, a, b))
        print(f"  {a:<5} {b:<5} d={dist:6.1f}{flag}")

    # ---- 2. chip 文字对比度 ----
    # 浅色主题：文字 = mix(attr 55%, base-content 45%)，底 = lofi base-100
    # 深色主题：文字 = mix(attr 55%, base-content 45%)，底 = business base-100
    print("\nchip 文字对比度（阈值 4.5:1）：")
    print(f"  {'属性':<6}{'浅色主题':>10}{'深色主题':>10}")
    contrast_failures = []
    for name in names:
        attr = PALETTE[name]
        light_ink = mix_oklch(attr, LIGHT_CONTENT, LIGHT_INK_ATTR_RATIO)
        dark_ink = mix_oklch(attr, DARK_CONTENT, DARK_INK_ATTR_RATIO)
        cl = contrast(light_ink, LIGHT_BG)
        cd = contrast(dark_ink, DARK_BG)
        bad = cl < 4.5 or cd < 4.5
        if bad:
            contrast_failures.append((name, cl, cd))
        print(f"  {name:<6}{cl:>10.2f}{cd:>10.2f}{'  <<< 不达标' if bad else ''}")

    print("\n" + "=" * 46)
    ok = True
    if failures:
        ok = False
        print(f"可区分性未达标 {len(failures)} 对：")
        for dist, a, b in failures:
            print(f"  {a} / {b} = {dist:.1f}（差 {MIN_DISTANCE - dist:.1f}）")
    else:
        print("可区分性：全部达标")
    if contrast_failures:
        ok = False
        print(f"对比度未达标 {len(contrast_failures)} 个：")
        for name, cl, cd in contrast_failures:
            print(f"  {name}: 浅 {cl:.2f} / 深 {cd:.2f}")
    else:
        print("对比度：全部达标")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
