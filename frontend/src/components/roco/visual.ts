/** 洛克王国视觉相关的共享工具。
 *
 *  属性配色集中在这里，而不是散在各个组件里——18 个属性在精灵卡片、
 *  阵容卡片、成员详情、技能列表里都要用，散开写必然会出现"同一个属性
 *  在不同页面颜色不一样"。
 */

/** 属性 → CSS 变量 slug。
 *
 *  用英文 slug 而不是直接把中文写进变量名：CSS 自定义属性虽然允许中文，
 *  但 `var(--attr-火)` 在压缩、转义、工具链处理时都容易出问题，
 *  而且 slug 与 style.css 里的定义能一一对照。
 *
 *  `无` 归到 `normal`：它是"无属性"，观感上就是中性灰。
 */
const ATTRIBUTE_SLUG: Record<string, string> = {
  普通: 'normal',
  草: 'grass',
  火: 'fire',
  水: 'water',
  光: 'light',
  地: 'ground',
  冰: 'ice',
  龙: 'dragon',
  电: 'electric',
  毒: 'poison',
  虫: 'bug',
  武: 'fighting',
  翼: 'flying',
  萌: 'fairy',
  幽: 'ghost',
  恶: 'dark',
  幻: 'psychic',
  机械: 'steel',
  无: 'normal',
}

/** 未知属性的兜底色（中性灰），避免出现"没有颜色"的元素。 */
const FALLBACK_SLUG = 'normal'

/** 属性 → 内联样式，供 `:style` 绑定。
 *
 *  用法：`<span class="attr-chip" :style="attributeStyle(pet.attributes[0])">`
 *
 *  **不要**在这里返回具体颜色值。返回 `var(--attr-xxx)` 引用，
 *  是为了让明暗主题切换、以及将来调整色板时，只改 style.css 一处。
 */
export function attributeStyle(type: string): Record<string, string> {
  const slug = ATTRIBUTE_SLUG[type] || FALLBACK_SLUG
  return { '--attr': `var(--attr-${slug})` }
}

/** 属性 → 具体颜色值（`var(...)` 形式）。
 *
 *  给不能用 CSS 变量的场景用（例如 canvas、需要拼进字符串的地方）。
 *  正常渲染一律用 `attributeStyle`。
 */
export function attributeColor(type: string): string {
  const slug = ATTRIBUTE_SLUG[type] || FALLBACK_SLUG
  return `var(--attr-${slug})`
}

/** 六维能力的中文名与字段映射。顺序固定，界面上不跳。 */
export const STAT_FIELDS = [
  { key: 'hp', label: '生命' },
  { key: 'atk', label: '物攻' },
  { key: 'sp_atk', label: '魔攻' },
  { key: 'def', label: '物防' },
  { key: 'sp_def', label: '魔防' },
  { key: 'spd', label: '速度' },
] as const

/** 个体值字段（iv_config 用的键名与 stats 不同）。 */
export const IV_FIELDS = [
  { key: 'hp', label: '生命' },
  { key: 'atk', label: '物攻' },
  { key: 'def', label: '物防' },
  { key: 'spatk', label: '魔攻' },
  { key: 'spdef', label: '魔防' },
  { key: 'speed', label: '速度' },
] as const

/** 种族值总和的分档，用于给用户一个"这只精灵强不强"的直观参考。
 *
 *  ⚠️ 阈值是**按当前数据的分布**定的（465 只里大部分在 400-700），
 *  不是官方分级。界面上标注清楚，别让用户以为是权威强度榜。
 *
 *  返回的是**中性色阶**（不用红/绿那种带评价性的强对比）：
 *  红绿会让人误以为这是官方强度评级，而它只是本项目的经验分档。
 */
export function statTier(total: number): { label: string; cls: string } {
  if (total >= 700) return { label: '顶级', cls: 'font-semibold' }
  if (total >= 600) return { label: '优秀', cls: '' }
  if (total >= 500) return { label: '良好', cls: 'opacity-80' }
  if (total > 0) return { label: '普通', cls: 'opacity-60' }
  return { label: '未知', cls: 'opacity-40' }
}

/** 六维数值 → 强度条的颜色。
 *
 *  与 `statTier` 一样刻意保持中性：用主色 + 透明度分档，
 *  而不是红→绿渐变。红绿会被读成"官方评级"，这里只是相对强弱。
 */
export function statBarClass(value: number): string {
  if (value >= 130) return 'bg-primary'
  if (value >= 100) return 'bg-primary/75'
  if (value >= 70) return 'bg-primary/55'
  return 'bg-primary/35'
}

/** 图片加载失败时的占位：用首字显示，避免一片空白。 */
export function initialOf(name: string): string {
  return (name || '?').trim().charAt(0)
}
