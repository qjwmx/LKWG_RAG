<script setup lang="ts">
import { computed, ref } from 'vue'
import { attributeStyle, initialOf } from './visual'

/** 精灵立绘。
 *
 *  刻意**不给图片加圆形容器**：BWIKI 的立绘是**透明底 PNG**，
 *  套 `rounded-full` + `overflow-hidden` 会把四角裁掉，
 *  精灵的耳朵、尾巴、翅膀都会被切。直接渲染图片，投影自然贴合轮廓。
 *
 *  只有加载失败时才退化到一个属性色圆块 + 首字。
 */

const props = withDefaults(
  defineProps<{
    name: string
    /** 图片 URL。空串或加载失败时退化为首字占位。 */
    src?: string
    /** 主属性，用于占位块的配色 */
    attribute?: string
    size?: 'xs' | 'sm' | 'md' | 'lg' | 'xl'
    /** 入场动画的错峰序号（由列表的 index 下发） */
    index?: number
  }>(),
  { src: '', attribute: '', size: 'md', index: -1 },
)

/** 图片加载失败要退化到占位，而不是显示浏览器的裂图图标。
 *  BWIKI 是热链，断网或图被删都会走到这里。 */
const failed = ref(false)

const showImage = computed(() => !!props.src && !failed.value)

/** 尺寸：立绘是主体，所以比旧版整体放大一档。 */
const sizeClass = computed(
  () =>
    ({
      xs: 'h-7 w-7 text-[10px]',
      sm: 'h-10 w-10 text-xs',
      md: 'h-14 w-14 text-sm',
      lg: 'h-20 w-20 text-lg',
      xl: 'h-28 w-28 text-2xl',
    })[props.size],
)

/** 属性色变量，挂在最外层容器上。
 *
 *  **只挂变量、不挂背景色**：立绘是透明底 PNG，若外层容器带背景色，
 *  精灵背后会多出一个色块（看起来像"贴纸"而不是立绘）。
 *  背景色只在加载失败的占位块上才加。 */
const attrVars = computed(() => attributeStyle(props.attribute || ''))

/** 占位块自己的配色（仅加载失败时用）。 */
const placeholderStyle = computed(() => ({
  backgroundColor: 'color-mix(in oklch, var(--attr) 18%, transparent)',
  color: 'color-mix(in oklch, var(--attr) 60%, var(--color-base-content))',
}))
</script>

<template>
  <div
    class="relative flex shrink-0 items-center justify-center"
    :class="[sizeClass, index >= 0 ? 'rise-in' : '']"
    :style="index >= 0 ? { ...attrVars, '--i': String(index) } : attrVars"
  >
    <img
      v-if="showImage"
      :src="src"
      :alt="name"
      loading="lazy"
      decoding="async"
      class="h-full w-full object-contain"
      @error="failed = true"
    />
    <!-- 占位：首字 + 属性色底。比裂图或空白都好。 -->
    <div
      v-else
      class="flex h-full w-full items-center justify-center rounded-full font-semibold"
      :style="placeholderStyle"
    >
      {{ initialOf(name) }}
    </div>
  </div>
</template>
