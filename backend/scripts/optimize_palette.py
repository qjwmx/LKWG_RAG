"""用局部搜索优化 18 属性色板：最大化最小两两距离，同时保证对比度达标。

为什么用搜索而不是手调
--------------------
18 个属性有 153 个两两组合，手调一个往往弄坏另一个——实测第一版就有
2 对距离不达标、3 个对比度不达标。这类"多个约束同时满足"的问题，
靠眼睛迭代会来回震荡。

搜索目标
-------
1. 最小化惩罚：任何一对距离 < 110 都罚（缺口越大罚越重）
2. 约束：chip 文字在明暗两套主题下对比度都 ≥ 4.5:1
3. 尽量保持色相贴近"游戏观感"（火偏红、水偏蓝…），
   所以**只允许小幅调整 L 和 H**，C 固定，避免调出反直觉的颜色。

用法::

    python -m scripts.optimize_palette
"""

from __future__ import annotations

import itertools
import json
import math
import random
import sys
from pathlib import Path

from scripts.verify_palette import (
    BLACK,
    DARK_BG,
    DARK_INK_ATTR_RATIO,
    LIGHT_BG,
    LIGHT_INK_ATTR_RATIO,
    MIN_DISTANCE,
    WHITE,
    contrast,
    mix_oklch,
    weighted_distance,
)

# 起始色板：色相按游戏观感固定，L 与 C 给一个合理初值
SEED: dict[str, tuple[float, float, float]] = {
    "普通": (72.0, 0.006, 0.0),
    "草":   (64.0, 0.170, 145.0),
    "火":   (60.0, 0.200, 32.0),
    "水":   (58.0, 0.150, 248.0),
    "光":   (80.0, 0.150, 78.0),
    "地":   (52.0, 0.100, 62.0),
    "冰":   (84.0, 0.090, 195.0),
    "龙":   (46.0, 0.180, 278.0),
    "电":   (88.0, 0.190, 112.0),
    "毒":   (52.0, 0.210, 318.0),
    "虫":   (74.0, 0.170, 128.0),
    "武":   (44.0, 0.190, 18.0),
    "翼":   (68.0, 0.120, 238.0),
    "萌":   (78.0, 0.130, 352.0),
    "幽":   (40.0, 0.140, 288.0),
    "恶":   (38.0, 0.030, 45.0),
    "幻":   (64.0, 0.240, 332.0),
    "机械": (54.0, 0.045, 235.0),
}

# 色相可调范围（度）与明度可调范围（%），限制幅度以保住"观感"
HUE_RANGE = 14.0
L_RANGE = 16.0


def ink_contrast(color: tuple[float, float, float]) -> tuple[float, float]:
    light_ink = mix_oklch(color, BLACK, LIGHT_INK_ATTR_RATIO)
    dark_ink = mix_oklch(color, WHITE, DARK_INK_ATTR_RATIO)
    return contrast(light_ink, LIGHT_BG), contrast(dark_ink, DARK_BG)


def cost(palette: dict[str, tuple[float, float, float]]) -> tuple[float, float]:
    """返回 (距离惩罚, 对比度惩罚)。字典序比较——先保证可区分，再保证可读。"""
    dist_penalty = 0.0
    for a, b in itertools.combinations(palette, 2):
        d = weighted_distance(palette[a], palette[b])
        if d < MIN_DISTANCE:
            dist_penalty += (MIN_DISTANCE - d) ** 2

    contrast_penalty = 0.0
    for name, color in palette.items():
        cl, cd = ink_contrast(color)
        for value in (cl, cd):
            if value < 4.5:
                contrast_penalty += (4.5 - value) ** 2 * 400.0  # 可读性优先，权重大
    return dist_penalty, contrast_penalty


def main() -> int:
    random.seed(20261001)
    names = list(SEED)
    # 搜索变量：每个属性的 L 与 H 相对种子的偏移
    offsets = {n: [0.0, 0.0] for n in names}

    def build(off: dict[str, list[float]]) -> dict[str, tuple[float, float, float]]:
        return {
            n: (
                SEED[n][0] + off[n][0],
                SEED[n][1],
                (SEED[n][2] + off[n][1]) % 360,
            )
            for n in names
        }

    best = build(offsets)
    best_cost = cost(best)
    print(f"起始：距离惩罚={best_cost[0]:.1f}  对比度惩罚={best_cost[1]:.1f}")

    temperature = 6.0
    for step in range(60000):
        if step % 6000 == 0 and step:
            temperature = max(0.4, temperature * 0.62)

        name = random.choice(names)
        axis = random.randrange(2)
        limit = L_RANGE if axis == 0 else HUE_RANGE
        delta = random.gauss(0, temperature)
        old = offsets[name][axis]
        new = max(-limit, min(limit, old + delta))
        offsets[name][axis] = new

        candidate = build(offsets)
        c = cost(candidate)
        # 字典序：先比距离惩罚，再比对比度惩罚
        if c < best_cost or random.random() < 0.002:
            if c <= best_cost:
                best_cost = c
                best = candidate
        else:
            offsets[name][axis] = old

    dist_penalty, contrast_penalty = best_cost
    print(f"结束：距离惩罚={dist_penalty:.1f}  对比度惩罚={contrast_penalty:.1f}")
    print()

    pairs = sorted(
        (weighted_distance(best[a], best[b]), a, b) for a, b in itertools.combinations(names, 2)
    )
    print(f"最小距离：{pairs[0][0]:.1f}（{pairs[0][1]} / {pairs[0][2]}），阈值 {MIN_DISTANCE}")
    print("最接近的 6 对：")
    for d, a, b in pairs[:6]:
        print(f"  {a:<5} {b:<5} d={d:6.1f}{'  <<< 不达标' if d < MIN_DISTANCE else ''}")

    print("\n对比度：")
    worst = []
    for name in names:
        cl, cd = ink_contrast(best[name])
        worst.append((min(cl, cd), name, cl, cd))
    worst.sort()
    for m, name, cl, cd in worst[:6]:
        print(f"  {name:<5} 浅 {cl:5.2f}  深 {cd:5.2f}{'  <<< 不达标' if m < 4.5 else ''}")

    print("\n最终色板（可直接粘贴进 style.css）：")
    for name in names:
        L, C, H = best[name]
        print(f"  --attr-{name}: oklch({L:.1f}% {C:.3f} {H:.1f});")

    # 同时落一份 JSON：Windows 终端编码会把中文打成乱码，
    # 手抄一遍极易出错。落盘再用编辑器读，避免转录错误。
    out = Path(__file__).resolve().parent.parent / "data" / "attr_palette.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "min_distance": round(pairs[0][0], 1),
                "threshold": MIN_DISTANCE,
                "palette": {
                    n: {
                        "oklch": f"oklch({best[n][0]:.1f}% {best[n][1]:.3f} {best[n][2]:.1f})",
                        "L": round(best[n][0], 1),
                        "C": round(best[n][1], 3),
                        "H": round(best[n][2], 1),
                    }
                    for n in names
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n已写入 {out}")
    return 0 if dist_penalty == 0 and contrast_penalty == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
