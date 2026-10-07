"""把散落的低对比度 opacity-* 文字位点换成语义化层级类。

为什么需要脚本而不是手改
-----------------------
有 140+ 处，手改一定会漏。但**也不能无脑全局替换**：
opacity-* 在项目里承担两种完全不同的职责，混在一起来替换会把界面改坏。

必须区分的两类
--------------
A. **文字层级**（要换）：`<p class="text-xs opacity-60">` ——
   opacity 只是用来"让这行字淡一点"。换成 ink-muted / ink-subtle。

B. **非文字用途**（绝不能换）：
   - 装饰性图标：`<svg class="... opacity-35">`
   - 禁用态 / 未激活：`opacity-40`（表示"不可用"）
   - hover 显隐：`opacity-0 group-hover:opacity-100`（删除按钮）
   - 加载指示：`loading loading-dots`
   - 骨架屏：`skeleton`
   - 纯装饰数字：404 页的 `opacity-20`

判据：这类元素的文字要么不存在，要么其对比度本来就不该按 WCAG 判定
（禁用控件、纯图标），换了反而破坏语义。

用法::

    python scripts/retier_text.py --dry-run   # 先看要改哪些
    python scripts/retier_text.py --apply
"""

from __future__ import annotations

import argparse
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent / "frontend" / "src"

# 档位映射：原有 5 档压成 2 档语义层级。
# 压档是被对比度下限逼出来的——business 主题在壁纸 65% 时，
# 文字不透明度低于 0.75 就不达标（见 glass_budget.py --floor）。
TIERS = {
    "45": "ink-subtle",  # 0.78 —— 壁纸开到上限仍 ≥5.0:1
    "50": "ink-subtle",
    "55": "ink-subtle",
    "60": "ink-muted",   # 0.86
    "65": "ink-muted",
    "70": "ink-muted",
}

# 出现这些片段的行**跳过**：opacity 在这里不是文字层级
SKIP_MARKERS = (
    "group-hover:",
    "hover:opacity-",
    "transition-opacity",
    "focus:opacity-",
    "disabled:opacity-",
    "loading loading-",
    "status status-",
    "skeleton",
    "divider",
    "aura",
    "<svg",
    "<img",
    "progress",
)

# 按文件 + 行号跳过：这些位置的 opacity 承担的是"颜色/状态"职责，
# 换成 ink-* 会**静默覆盖**掉原有颜色或状态语义。
SKIP_LINES: dict[str, set[int]] = {
    # AttributeChip 的计数坐在 attr-chip 内部，文字色由 --attr 派生。
    # 换成 ink-muted 会把属性色覆盖成灰，计数与属性名脱节。
    "components/roco/AttributeChip.vue": {37},
}

OP_RE = re.compile(r"\bopacity-(\d{1,3})\b")


def process_line(line: str) -> tuple[str, list[str]]:
    """返回 (新行, 实际替换掉的档位列表)。"""
    if any(marker in line for marker in SKIP_MARKERS):
        return line, []

    replaced: list[str] = []

    def sub(match: re.Match[str]) -> str:
        value = match.group(1)
        tier = TIERS.get(value)
        if tier is None:
            return match.group(0)
        replaced.append(value)
        return tier

    new_line = OP_RE.sub(sub, line)
    return new_line, replaced


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只预览）")
    parser.add_argument("--dry-run", action="store_true", help="只预览")
    args = parser.parse_args()

    apply = args.apply and not args.dry_run

    total_files = 0
    total_sites = 0
    report: list[str] = []

    for path in sorted(ROOT.rglob("*.vue")):
        original = path.read_text(encoding="utf-8")
        lines = original.splitlines(keepends=True)
        changed_any = False

        for i, line in enumerate(lines):
            rel = str(path.relative_to(ROOT)).replace("\\", "/")
            if i + 1 in SKIP_LINES.get(rel, set()):
                continue
            new_line, replaced = process_line(line)
            if not replaced:
                continue
            changed_any = True
            total_sites += len(replaced)
            report.append(
                f"{path.relative_to(ROOT.parent.parent)}:{i + 1}  "
                f"opacity-{','.join(replaced)} → {new_line.strip()[:96]}"
            )
            lines[i] = new_line

        if changed_any:
            total_files += 1
            if apply:
                path.write_text("".join(lines), encoding="utf-8")

    for row in report:
        print(row)

    print()
    print(f"{'已写入' if apply else '预览（未写入）'}："
          f"{total_files} 个文件、{total_sites} 处")
    if not apply:
        print("确认无误后加 --apply 真正写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
