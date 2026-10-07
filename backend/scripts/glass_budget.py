"""求解并验收「毛玻璃 + 壁纸」的对比度预算。

为什么需要先算再写 CSS
---------------------
毛玻璃把"文字压在什么底色上"从**编译期常量**变成了**运行期变量**：
底色的亮度取决于用户选的壁纸，而壁纸是任意图片。

两套主题的余量差了三倍多（这是本文件最重要的结论）：

    lofi（浅色）     base-100 = 纯白、base-content = 纯黑 → 21.00:1
    business（深色） base-100 = oklch(24.35%)、base-content = oklch(84.87%) → 10.26:1

所以"面板不透明度"必须**按主题分别取值**：浅色可以薄（壁纸看得清），
深色必须厚（否则浅色字压在浅色壁纸上直接看不见）。

合成模型
--------
全部按 **sRGB 空间**合成（浏览器合成半透明 background-color 就是在
设备色彩空间做的，不是线性空间）。三层：

    page  = X·wp + (1-X)·B        X = 壁纸不透明度，wp = 壁纸像素亮度
    panel = a·B + (1-a)·page      a = 面板（玻璃画布）不透明度
    fg    = m·C + (1-m)·panel     m = 文字不透明度，C = base-content 亮度

要求 contrast(fg, panel) ≥ 4.5（WCAG AA 正文）。

最坏情况取"壁纸与主题底色方向相反"：
- 浅色主题 + 全黑壁纸（把面板拉暗 → 黑字看不清）
- 深色主题 + 全白壁纸（把面板拉亮 → 浅字看不清）

这也解释了为什么 ``backdrop-filter: blur()`` 不能只靠"模糊了就好"：
模糊只破坏壁纸的**局部**对比（把锐利边缘变成平滑过渡），
但整块壁纸的平均亮度不会变。所以上面的均匀模型是保守且正确的上界。

用法::

    python -m scripts.glass_budget            # 打印预算表与推荐工作点
    python -m scripts.glass_budget --verify   # 按当前设计参数验收
"""

from __future__ import annotations

import argparse


def srgb_lin(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lum(c: float) -> float:
    """灰阶 sRGB（0-1）→ WCAG 相对亮度。"""
    return srgb_lin(c)


def contrast(fg: float, bg: float) -> float:
    l1, l2 = lum(fg), lum(bg)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def oklch_L_to_srgb(lightness: float) -> float:
    """oklch 的 L 通道（0-1）→ sRGB 灰阶值。

    对中性色，L 的立方近似等于线性亮度，再经 sRGB 传输函数回到 0-1 的表示。
    用它把 daisyUI 主题里的 oklch(24.353% 0 0) 转成可比较的 sRGB 值。
    """
    lin = lightness**3
    return 12.92 * lin if lin <= 0.0031308 else 1.055 * lin ** (1 / 2.4) - 0.055


# daisyUI 主题锚点（与 frontend/node_modules/daisyui/theme/*.css 一致）
THEMES = {
    "lofi": {
        "B": 1.0,  # --color-base-100: oklch(100% 0 0)
        "C": 0.0,  # --color-base-content: oklch(0% 0 0)
        "wp": 0.0,  # 最坏壁纸：把面板拉暗的方向
        "wp_label": "全黑壁纸",
    },
    "business": {
        "B": oklch_L_to_srgb(0.24353),
        "C": oklch_L_to_srgb(0.84870),
        "wp": 1.0,  # 最坏壁纸：把面板拉亮的方向
        "wp_label": "全白壁纸",
    },
}

# ---------------------------------------------------------------- 设计参数
# 这些值必须与 frontend/src/style.css 里的 --glass-* / --surface-* / --wallpaper-*
# 保持一致；改任意一边都要重跑 --verify。
#
# 结构（自下而上）：
#   wallpaper  清晰可见的背景图，**不做任何模糊**
#   chrome     顶栏 / 侧栏：文字**直接**压在上面，所以必须够实
#   surface    内容区里的卡片：文字压在这层上，必须够实
#   panel      浮层（菜单/弹窗）：面积小，自己再 blur 一次
#
# ★ 内容区**没有**玻璃层——这是第三次修正，也是最终结论
# --------------------------------------------------------
# 演进过程（每一步都被实测否掉，记下来免得再走回头路）：
#
#   1. 全屏 .glass-canvas 对整个壁纸做模糊
#      → 壁纸变成一片灰雾，选哪张都一样
#   2. 内容区做 58%/72% 的整块玻璃
#      → 壁纸可见份额只剩 18.9%/12.6%，用户说"上传了没生效"
#   3. 内容区降到 28%/40%（本次之前的版本）
#      → **平均色差通过了**（色差 25~80），但用户仍说"看不见壁纸"
#
# 第 3 步为什么还是不行：平均色差只说明"颜色变了"，不说明"能认出这是一张图"。
# 用 80px 棋盘格实测（scripts/probe_wallpaper_detail.py）：
#
#     配置                        R−B 标准差   纯色点比例
#     region 有 blur（28%）            54.2          0%   ← 只剩色晕
#     region 不 blur（28%）           133.2          0%   ← 仍是色晕！
#     region 完全去掉                 184.4         86%   ← 结构清晰
#
# 关键发现：**即使不模糊，28% 的白色填充也足以把壁纸冲淡**。
# 只要内容区铺一层半透明白，壁纸就永远不可能清晰——
# 而它铺满整个视口，所以壁纸在任何地方都不清晰。
#
# 所以内容区**不能有玻璃层**：壁纸原样显示，可读性完全交给卡片。
DESIGN = {
    "layers": {
        # chrome = 顶栏/侧栏（文字直接压在上面）
        # surface = 内容区卡片（文字压在这层上）
        # 注意这里**没有 region**：内容区不加任何玻璃。
        "lofi": {"chrome": 0.62, "surface": 0.55},
        "business": {"chrome": 0.82, "surface": 0.86},
    },
    # 壁纸浓度：默认值与滑杆上限
    #
    # 内容区没有玻璃之后，壁纸是 1:1 原样显示的，所以浓度可以开得很高。
    # 上限由 **chrome（顶栏/侧栏）** 决定，不是内容区：
    # business 的 chrome 是 82%，实测最多支撑到 87% 浓度
    # （再高时顶栏上的最弱文字只有 4.20:1）。所以取 85% 留一点余量。
    "wallpaper_default": 0.85,
    "wallpaper_max": 0.85,
    # 次要文字不透明度下限。所有 opacity-* 文字都必须 ≥ 这个值。
    "muted_floor": 0.78,
    # 实际在用的文字层级（用于逐档验收）
    "text_tiers": {"正文": 1.0, "次要": 0.86, "最弱": 0.78},
    "need": 4.5,
}


def stack_luminance(theme: dict, X: float, alphas: list[float]) -> float:
    """按顺序合成若干半透明玻璃层，返回最坏情况下最终底色亮度。

    alphas 从下到上排列（先全屏画布、再卡片、再卡片上的浮层…）。
    每层都往主题底色 B 混，所以层数越多、壁纸越被盖住。
    """
    B = theme["B"]
    page = X * theme["wp"] + (1 - X) * B
    value = page
    for a in alphas:
        value = a * B + (1 - a) * value
    return value


def text_contrast(theme: dict, X: float, alphas: list[float], m: float) -> float:
    bg = stack_luminance(theme, X, alphas)
    fg = m * theme["C"] + (1 - m) * bg
    return contrast(fg, bg)


def alphas_for(theme_name: str, where: str = "chrome") -> list[float]:
    """取某主题某处的玻璃层栈。

    where:
      "chrome"  顶栏/侧栏上的文字 —— 底下只有 chrome 那一层
      "surface" 内容区卡片上的文字 —— 只有卡片那一层
                （内容区**没有**玻璃层，所以卡片底下就是壁纸）

    注意这里**没有 "region"**：内容区不加任何玻璃，见 DESIGN 的说明。
    """
    layers = DESIGN["layers"][theme_name]
    if where == "chrome":
        return [layers["chrome"]]
    return [layers["surface"]]


def max_wallpaper_for(theme: dict, theme_name: str, m: float,
                      where: str = "chrome", need: float = 4.5) -> float:
    """给定文字不透明度 m，壁纸最多能开到多少。

    单调性：壁纸越不透明，面板越偏离主题底色，对比度越低，所以二分即可。
    返回 -1 表示"连不显示壁纸都不达标"。
    """
    alphas = alphas_for(theme_name, where)
    if text_contrast(theme, 0.0, alphas, m) < need:
        return -1.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if text_contrast(theme, mid, alphas, m) >= need:
            lo = mid
        else:
            hi = mid
    return lo


def print_anchors() -> None:
    print("=" * 76)
    print("主题锚点（sRGB 灰阶）—— 余量差距是全局设计的起点")
    print("=" * 76)
    for name, t in THEMES.items():
        print(
            f"  {name:<10} base-100={t['B']:.4f}  base-content={t['C']:.4f}"
            f"  满对比度={contrast(t['C'], t['B']):5.2f}:1   最坏壁纸={t['wp_label']}"
        )
        for m in (0.68, 0.78, 0.86, 1.0):
            on_base = contrast(m * t["C"] + (1 - m) * t["B"], t["B"])
            print(f"      文字不透明度 {m:.2f} → 纯底色上 {on_base:5.2f}:1")


def print_budget_table() -> None:
    """区域玻璃不透明度 × 文字不透明度 → 壁纸最多能开到多少。"""
    print()
    print("=" * 76)
    print("区域玻璃不透明度 × 文字不透明度 → 壁纸最多能开多少（两主题都要 ≥4.5:1）")
    print("=" * 76)
    print(f"{'a':>6}{'m':>6}  {'lofi':>8}{'business':>10}{'取小':>8}   判定")
    print("-" * 76)
    for a in (0.45, 0.50, 0.58, 0.65, 0.72, 0.82):
        for m in (0.68, 0.78, 0.86, 1.0):
            caps = {
                n: _cap_for_alpha(t, a, m) for n, t in THEMES.items()
            }
            limit = min(caps.values())
            print(
                f"{a:>6.2f}{m:>6.2f}  {caps['lofi'] * 100:>7.0f}%"
                f"{caps['business'] * 100:>9.0f}%{limit * 100:>7.0f}%   "
                f"{'可用' if limit >= 0.5 else '壁纸几乎看不见'}"
            )


def _cap_for_alpha(theme: dict, a: float, m: float, need: float = 4.5) -> float:
    """指定单层不透明度 a 时，壁纸最多能开到多少（用于预算表）。"""
    if text_contrast(theme, 0.0, [a], m) < need:
        return -1.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if text_contrast(theme, mid, [a], m) >= need:
            lo = mid
        else:
            hi = mid
    return lo


def verify() -> int:
    """按当前设计参数逐档验收。返回非 0 表示不达标。"""
    print("=" * 76)
    print("验收：当前设计参数（与 style.css 的 --glass-* / --surface-* / --wallpaper-* 对应）")
    print("=" * 76)
    for n, layers in DESIGN["layers"].items():
        print(f"  {n:<10} chrome={layers['chrome']}  surface={layers['surface']}"
              "  region=（无，内容区不加玻璃）")
    print(f"  壁纸浓度         : 默认 {DESIGN['wallpaper_default']}，上限 {DESIGN['wallpaper_max']}")
    print(f"  次要文字下限     : {DESIGN['muted_floor']}")
    print()

    failures: list[str] = []

    # 1) 两处文字在「默认壁纸」和「最大壁纸」下都要达标。
    WHERE_LABEL = {"chrome": "顶栏/侧栏", "surface": "卡片上"}
    for theme_name, theme in THEMES.items():
        print(f"  ── {theme_name}（最坏壁纸={theme['wp_label']}）──")
        for where in ("chrome", "surface"):
            alphas = alphas_for(theme_name, where)
            for label, X in (("默认壁纸", DESIGN["wallpaper_default"]),
                             ("最大壁纸", DESIGN["wallpaper_max"])):
                bg = stack_luminance(theme, X, alphas)
                cells = []
                for tier, m in DESIGN["text_tiers"].items():
                    c = text_contrast(theme, X, alphas, m)
                    mark = "OK" if c >= DESIGN["need"] else "不足"
                    if c < DESIGN["need"]:
                        failures.append(
                            f"[{theme_name}] {WHERE_LABEL[where]} {label} {tier}文字(m={m}) "
                            f"对比度 {c:.2f} < {DESIGN['need']}"
                        )
                    cells.append(f"{tier} {c:5.2f} {mark}")
                print(f"       {WHERE_LABEL[where]:<10} {label:<6} 底色亮度={bg:.4f}   "
                      + "  ".join(cells))
        print()

    # 2) chrome 与 surface 各自能支撑的壁纸上限
    print("  ── 可支撑的壁纸上限（取两主题中更严的）──")
    for where in ("chrome", "surface"):
        caps = {n: max_wallpaper_for(t, n, DESIGN["muted_floor"], where, DESIGN["need"])
                for n, t in THEMES.items()}
        limit = min(caps.values())
        ok = limit >= DESIGN["wallpaper_max"]
        if not ok:
            failures.append(
                f"{where} 处最弱文字壁纸只能开到 {limit * 100:.0f}%，"
                f"低于设定上限 {DESIGN['wallpaper_max'] * 100:.0f}%"
            )
        print(f"       {where:<10} lofi {caps['lofi'] * 100:3.0f}%  "
              f"business {caps['business'] * 100:3.0f}%  → 取严 {limit * 100:3.0f}%  "
              f"{'OK' if ok else '不足'}")
    print()

    # 3) 无壁纸时必须完全等同原设计（回归保证）
    print("  ── 无壁纸（X=0）回归 ──")
    for theme_name, theme in THEMES.items():
        parts = []
        for where in ("chrome", "surface"):
            c = text_contrast(theme, 0.0, alphas_for(theme_name, where),
                              DESIGN["muted_floor"])
            if c < DESIGN["need"]:
                failures.append(
                    f"[{theme_name}] 无壁纸（{WHERE_LABEL[where]}）最弱文字 "
                    f"对比度 {c:.2f} < 4.5"
                )
            parts.append(f"{WHERE_LABEL[where]} {c:5.2f}:1")
        print(f"       {theme_name:<10} " + "   ".join(parts))
    print()

    print("=" * 76)
    if failures:
        print(f"未通过 {len(failures)} 项：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("全部通过：默认与最大壁纸浓度下，顶栏/侧栏与卡片上的文字均 ≥ 4.5:1")
    return 0

def min_text_opacity(theme: dict, theme_name: str, X: float, where: str = "chrome",
                     need: float = 4.5) -> float:
    """给定壁纸浓度 X，文字不透明度的**下限**。

    单调递增：文字越实，对比度越高。二分求最小可行值。
    返回 >1 表示即使文字全实也不达标（玻璃太薄）。
    """
    alphas = alphas_for(theme_name, where)
    if text_contrast(theme, X, alphas, 1.0) < need:
        return 2.0
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if text_contrast(theme, X, alphas, mid) >= need:
            hi = mid
        else:
            lo = mid
    return hi


def print_floor_table() -> None:
    """核心决策表：壁纸浓度 ↔ 次要文字不透明度下限的取舍。

    分别列出「顶栏/侧栏」与「内容区卡片」两种情况——两者层栈不同，
    卡片多一层填充，所以对文字更宽容。

    ★ 这张表是"壁纸看不看得见"的判据：它给出"为了让文字达标，
    玻璃至少要多厚"，而玻璃越厚壁纸越看不见。两端的取舍就在这里。
    """
    print()
    print("=" * 76)
    print("壁纸浓度 → 次要文字不透明度**下限**（各主题用自己的设计值，取严）")
    print("=" * 76)
    print(f"{'壁纸':>7}{'顶栏 lofi':>11}{'顶栏 biz':>10}{'取严':>8}"
          f"{'卡片 lofi':>11}{'卡片 biz':>10}{'取严':>8}")
    print("-" * 76)
    for X in (0.0, 0.30, 0.50, 0.65, 0.75, 0.85, 1.0):
        chrome = {n: min_text_opacity(t, n, X, "chrome") for n, t in THEMES.items()}
        surface = {n: min_text_opacity(t, n, X, "surface") for n, t in THEMES.items()}
        print(
            f"{X * 100:>6.0f}%{chrome['lofi']:>11.2f}{chrome['business']:>10.2f}"
            f"{max(chrome.values()):>8.2f}"
            f"{surface['lofi']:>11.2f}{surface['business']:>10.2f}{max(surface.values()):>8.2f}"
        )
    print()
    print(f"  设计要求：最弱文字层 {DESIGN['muted_floor']}，"
          f"在壁纸浓度上限 {DESIGN['wallpaper_max'] * 100:.0f}% 下仍要达标。")
    print("  项目原有的 0.45/0.50/0.55/0.60/0.65 全部落在不可行区，必须上调。")
    print()
    print("  为什么 business 是瓶颈：它的 base-100 只有 oklch(24.35%)，")
    print("  底色本身就暗，白色壁纸一透过来就把玻璃拉亮，浅色字立刻被吃掉对比度。")
    print()
    print("  读法：这张表告诉你「玻璃至少要多厚文字才够清楚」，")
    print("  而玻璃越厚壁纸越看不见——所以它是「壁纸可见度」的反向指标。")


def print_visibility() -> None:
    """壁纸可见份额：本次修正的直接依据。

    「上传了但背景没变」的根因不是上传失败，而是**壁纸被玻璃冲淡了**：
    可见份额 = (1 - 玻璃不透明度) × 壁纸浓度。
    这个值低于约 25% 时，换任何壁纸都只是"一点点色偏"。
    """
    print()
    print("=" * 76)
    print("壁纸可见份额 = (1 - 玻璃不透明度) × 壁纸浓度")
    print("=" * 76)
    print(f"{'结构':<26}{'lofi':>10}{'business':>12}   判定")
    print("-" * 76)

    def share(theme_name: str, where: str, X: float) -> float:
        alphas = alphas_for(theme_name, where)
        # 多层玻璃的总遮盖率：各层乘起来
        cover = 1.0
        for a in alphas:
            cover *= a
        return (1 - cover) * X

    X = DESIGN["wallpaper_default"]
    rows = [
        ("旧方案：内容区整块玻璃", [0.58], [0.72]),
        ("新方案：内容区轻玻璃", [DESIGN["layers"]["lofi"]["region"]],
         [DESIGN["layers"]["business"]["region"]]),
        ("新方案：卡片（面积小）", alphas_for("lofi", "surface"),
         alphas_for("business", "surface")),
    ]
    for label, lo_layers, bi_layers in rows:
        def cov(layers: list[float]) -> float:
            c = 1.0
            for a in layers:
                c *= a
            return (1 - c) * X
        lo, bi = cov(lo_layers), cov(bi_layers)
        worst = min(lo, bi)
        print(f"{label:<26}{lo * 100:>9.1f}%{bi * 100:>11.1f}%   "
              f"{'明显可见' if worst >= 0.35 else ('可辨' if worst >= 0.25 else '★ 太弱')}")
    print()
    print("  参照：>= 35% 明显可见；25–35% 可辨；< 25% 基本只剩色偏，")
    print("        用户会认为「壁纸没生效」（这正是实际反馈的问题）。")


def print_sensitivity() -> None:
    """同一 a 套两主题会怎样——说明为什么必须分主题取值。"""
    print()
    print("=" * 76)
    print("反例：如果两主题用同一个 a（错误做法）")
    print("=" * 76)
    print(f"{'a':>6}{'壁纸 85% 时 lofi 可用':>26}{'壁纸 85% 时 business 可用':>28}")
    print("-" * 76)
    for a in (0.55, 0.62, 0.70, 0.82):
        caps = {n: _cap_for_alpha(t, a, DESIGN["muted_floor"]) for n, t in THEMES.items()}
        print(
            f"{a:>6.2f}{('是' if caps['lofi'] >= 0.85 else '否'):>26}"
            f"{('是' if caps['business'] >= 0.85 else '否'):>28}"
        )
    print()
    print("  取 a=0.62：lofi 绰绰有余，business 不达标。")
    print("  取 a=0.82：business 达标，但 lofi 的玻璃就厚得像不透明面板，壁纸白加了。")
    print("  → 只能分主题给 a，这正是 --glass-* 分两套的原因。")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="按当前设计参数验收")
    parser.add_argument("--floor", action="store_true", help="只打印取舍表")
    parser.add_argument("--visibility", action="store_true", help="只打印壁纸可见份额")
    args = parser.parse_args()

    if args.verify:
        return verify()

    if args.floor:
        print_floor_table()
        return 0

    if args.visibility:
        print_visibility()
        return 0

    print_anchors()
    print_budget_table()
    print_floor_table()
    print_visibility()
    print_sensitivity()
    print()
    print("=" * 76)
    print("结论")
    print("=" * 76)
    print("  1. 玻璃不透明度必须**按主题分别取值**：lofi 薄、business 厚。")
    print("     同一个值不可能同时满足两边——深色余量只有浅色的一半。")
    print("  2. 次要文字不透明度有硬下限。低于下限时，无论玻璃多厚都会不达标，")
    print("     因为文字自身被稀释了。这是本设计里最容易踩的坑：")
    print("     项目原有 112 处 opacity-50…65 在玻璃上会直接不达标。")
    print("  3. ★ 「壁纸看不见」通常不是上传失败，而是**被玻璃冲淡**：")
    print("     可见份额 = (1 - 玻璃不透明度) × 壁纸浓度，低于 25% 就等于没有。")
    print("     所以内容区（region）必须做得很轻，文字交给卡片（surface）承载。")
    print()
    return verify()


if __name__ == "__main__":
    raise SystemExit(main())
