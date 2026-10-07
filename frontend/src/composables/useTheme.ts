import { ref, watch } from 'vue'

const STORAGE_KEY = 'rag_theme'

/** 主题名与 daisyUI 启用的主题一一对应（见 style.css 的 @plugin "daisyui"）。
 *
 *  `lofi` 是浅色、`business` 是深色。两者都是官方内置主题，
 *  但**几何令牌与品牌色在 style.css 里被统一覆写**——
 *  官方主题各自的 radius 不一致（lofi 的 selector 是 2rem、business 是 0），
 *  直接混用会出现"浅色圆角大、深色圆角方"的割裂。
 */
export type ThemeName = 'lofi' | 'business'

/** 旧主题名 → 新主题名。
 *
 *  必须做这个映射：老用户的 localStorage 里存的是 `light` / `dark`，
 *  而这两个主题现在**没有被启用**——直接把 `light` 写进 data-theme，
 *  daisyUI 找不到匹配的变量集，页面会退化成完全无样式。
 *  这种故障看起来像"CSS 挂了"，很难联想到是主题名过期。
 */
const LEGACY: Record<string, ThemeName> = {
  light: 'lofi',
  dark: 'business',
  lofi: 'lofi',
  business: 'business',
}

function normalize(raw: string | null): ThemeName | null {
  if (!raw) return null
  return LEGACY[raw] ?? null
}

const stored = normalize(localStorage.getItem(STORAGE_KEY))
// 没存过（或存的是无法识别的值）就跟随系统；存过就以用户的显式选择为准
const initial: ThemeName =
  stored ?? (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'business' : 'lofi')

const theme = ref<ThemeName>(initial)

function apply(value: ThemeName): void {
  // daisyUI 5 靠 <html data-theme="..."> 切换主题。
  // style.css 里的 @custom-variant dark 也跟着它，所以 dark: 前缀会同步生效。
  document.documentElement.setAttribute('data-theme', value)
}

apply(theme.value)

watch(theme, (value) => {
  apply(value)
  localStorage.setItem(STORAGE_KEY, value)
})

export function useTheme() {
  function toggle(): void {
    theme.value = theme.value === 'business' ? 'lofi' : 'business'
  }
  return { theme, toggle }
}
