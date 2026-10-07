/** 内置壁纸。
 *
 * ★ 为什么壁纸必须是**饱和色**，不能用浅粉浅蓝那种淡色
 * ------------------------------------------------------
 * 这是实测 + 计算得出的结论，不是审美偏好。
 *
 * 毛玻璃会把壁纸往主题底色方向"压"。以浅色主题为例，
 * 壁纸色 W 透过不透明度 a=58% 的玻璃后变成：
 *
 *     玻璃后 = a·base + (1-a)·(X·W + (1-X)·base)
 *
 * 若 base 是纯白（lofi）、W 也接近白（比如 #e0f2fe），
 * 这个式子算出来几乎还是白 —— 实测色差只有 7.8，
 * 而人眼可辨约需 15、明显可见需 25。
 * 也就是说**淡色壁纸在玻璃后面等于不存在**，用户会以为"壁纸没生效"。
 *
 * 反过来，饱和色（如 #6366f1）即使透过 58% 玻璃，色差仍有 36.8，
 * 透过深色主题的 72% 玻璃也有 25.8 —— 是明显可见的。
 *
 * 结论：壁纸负责提供**色彩**，玻璃负责提供**质感**。
 * 淡色壁纸 + 厚玻璃 = 什么都看不到。
 *
 * 注意：用户**自己上传**的图片不受这个约束（照片通常本来就有足够对比），
 * 极端情况由 glass_budget.py 的"最坏壁纸"上界兜底。
 *
 * 另外，全部用 CSS 渐变而不是打包图片：
 * 1. **可验证**：颜色是我自己写的，亮度范围已知，能对上 glass_budget.py 的推导；
 *    换成照片就变成任意输入，只能靠最坏情况兜底。
 * 2. **零请求零体积**：一张 1920×1080 照片压缩后也有几百 KB，
 *    这里 8 条渐变合计不到 2 KB，且任意分辨率都不糊。
 * 3. **明暗两套**：照片很难同时适配浅色和深色主题；渐变可以分别给一版。
 */

export interface WallpaperPreset {
  id: string
  label: string
  /** 浅色主题下的 background-image 值 */
  light: string
  /** 深色主题下的 background-image 值 */
  dark: string
  thumbLight: string
  thumbDark: string
}

/** 网格纹理：Linear / Vercel 那种"技术感底纹"。
 *  用 repeating-linear-gradient 画 1px 线，比 SVG 或图片省得多。
 *  线本身要够明显，否则在玻璃后完全看不见（同"饱和色"的道理）。 */
function gridLines(rgb: string, size = 30): string {
  return (
    `repeating-linear-gradient(0deg, rgba(${rgb},0.16) 0 1px, transparent 1px ${size}px), ` +
    `repeating-linear-gradient(90deg, rgba(${rgb},0.16) 0 1px, transparent 1px ${size}px)`
  )
}

/** 由一组色标生成"双色斜向"渐变。
 *  两个色标都取饱和色，避免渐变中段落在接近底色的区域。 */
function duo(a: string, b: string, angle = 135): string {
  return `linear-gradient(${angle}deg, ${a} 0%, ${b} 100%)`
}

export const WALLPAPER_PRESETS: WallpaperPreset[] = [
  {
    id: 'indigo',
    label: '靛蓝',
    light: duo('#818cf8', '#6366f1'),
    dark: duo('#4338ca', '#6366f1'),
    thumbLight: duo('#818cf8', '#6366f1'),
    thumbDark: duo('#4338ca', '#6366f1'),
  },
  {
    id: 'cyan',
    label: '青蓝',
    light: duo('#38bdf8', '#0ea5e9'),
    dark: duo('#0369a1', '#0ea5e9'),
    thumbLight: duo('#38bdf8', '#0ea5e9'),
    thumbDark: duo('#0369a1', '#0ea5e9'),
  },
  {
    id: 'emerald',
    label: '翠绿',
    light: duo('#34d399', '#10b981'),
    dark: duo('#059669', '#10b981'),
    thumbLight: duo('#34d399', '#10b981'),
    thumbDark: duo('#059669', '#10b981'),
  },
  {
    id: 'violet',
    label: '紫罗兰',
    light: duo('#a78bfa', '#8b5cf6'),
    dark: duo('#6d28d9', '#8b5cf6'),
    thumbLight: duo('#a78bfa', '#8b5cf6'),
    thumbDark: duo('#6d28d9', '#8b5cf6'),
  },
  {
    id: 'rose',
    label: '玫红',
    light: duo('#fb7185', '#f43f5e'),
    dark: duo('#9f1239', '#f43f5e'),
    thumbLight: duo('#fb7185', '#f43f5e'),
    thumbDark: duo('#9f1239', '#f43f5e'),
  },
  {
    id: 'amber',
    label: '琥珀',
    light: duo('#fbbf24', '#f59e0b'),
    dark: duo('#b45309', '#f59e0b'),
    thumbLight: duo('#fbbf24', '#f59e0b'),
    thumbDark: duo('#b45309', '#f59e0b'),
  },
  {
    id: 'mesh',
    label: '光晕',
    // 两个 radial 光斑 + 一层饱和底色。光斑用同色相但更亮的一端，
    // 保证"有明暗变化"却不会落到接近底色的灰白区域。
    light:
      'radial-gradient(ellipse 65% 55% at 20% 15%, rgba(129,140,248,0.85), transparent 72%), ' +
      'radial-gradient(ellipse 60% 50% at 85% 90%, rgba(56,189,248,0.75), transparent 72%), ' +
      'linear-gradient(160deg, #818cf8, #38bdf8)',
    dark:
      'radial-gradient(ellipse 65% 55% at 20% 15%, rgba(99,102,241,0.85), transparent 72%), ' +
      'radial-gradient(ellipse 60% 50% at 85% 90%, rgba(14,165,233,0.70), transparent 72%), ' +
      'linear-gradient(160deg, #4338ca, #0369a1)',
    thumbLight:
      'radial-gradient(ellipse 65% 55% at 20% 15%, rgba(129,140,248,0.95), transparent 72%), ' +
      'linear-gradient(160deg, #818cf8, #38bdf8)',
    thumbDark:
      'radial-gradient(ellipse 65% 55% at 20% 15%, rgba(99,102,241,0.95), transparent 72%), ' +
      'linear-gradient(160deg, #4338ca, #0369a1)',
  },
  {
    id: 'grid',
    label: '网格',
    // 网格线用深色（浅色主题）/浅色（深色主题）。
    // 注意：模糊半径 20px 远大于 1px 线宽，单根线一定会被抹平，
    // 所以这里靠"线够密 + 对比够"保留纹理感，而不是靠线够粗。
    light: `${gridLines('49,46,129')}, linear-gradient(160deg, #818cf8, #6366f1)`,
    dark: `${gridLines('199,210,254')}, linear-gradient(160deg, #4338ca, #6366f1)`,
    thumbLight: `${gridLines('49,46,129', 10)}, linear-gradient(160deg, #818cf8, #6366f1)`,
    thumbDark: `${gridLines('199,210,254', 10)}, linear-gradient(160deg, #4338ca, #6366f1)`,
  },
]

export const NONE_ID = 'none'
export const CUSTOM_ID = 'custom'

export function findPreset(id: string): WallpaperPreset | undefined {
  return WALLPAPER_PRESETS.find((preset) => preset.id === id)
}

/** 按当前主题取该预设的 background-image 值。 */
export function presetCss(preset: WallpaperPreset, theme: 'lofi' | 'business'): string {
  return theme === 'business' ? preset.dark : preset.light
}

export function thumbCss(preset: WallpaperPreset, theme: 'lofi' | 'business'): string {
  return theme === 'business' ? preset.thumbDark : preset.thumbLight
}
