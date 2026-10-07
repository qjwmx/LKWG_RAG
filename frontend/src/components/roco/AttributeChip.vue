<script setup lang="ts">
import { computed } from 'vue'
import { attributeStyle } from './visual'

/** 属性标签。
 *
 *  为什么单独一个组件：18 个属性出现在精灵卡、阵容卡、成员详情、技能列表、
 *  筛选器等至少 8 处。散开写的话，"某个属性的底色/边框/文字色"会在各处漂移，
 *  而这类漂移很难靠肉眼在多个页面之间对比出来。
 *
 *  颜色全部来自 style.css 的 `--attr-*` 变量，本组件只负责挂上 `--attr`。
 *  这样调色板时只改一处，明暗主题也自动适配。
 */

const props = withDefaults(
  defineProps<{
    type: string
    /** xs 用于精灵卡（空间极小），sm 用于详情页与列表 */
    size?: 'xs' | 'sm'
    /** 选中态：实心属性色 + 反色文字（筛选器用） */
    active?: boolean
    /** 追加在属性名后的计数（筛选器用） */
    count?: number
  }>(),
  { size: 'sm', active: false, count: undefined },
)

const classes = computed(() => [
  props.size === 'xs' ? 'text-[10px] px-1' : '',
  props.active ? 'attr-chip-active' : '',
])
</script>

<template>
  <span class="attr-chip" :class="classes" :style="attributeStyle(type)" :title="type">
    {{ type }}
    <!-- 计数**不能用 opacity 淡化**。
         attr-chip 的文字色本身已经是 color-mix(--attr 60%, base-content)，
         再叠一层 opacity-70 等于稀释两次：实测在 business 主题下达 3.0:1，
         低于 WCAG AA 的 4.5:1（而且是"要读的数字"，不是装饰）。

         改用**背景药丸**区分层级：颜色保持全强度，靠浅底把数字与属性名分开。
         这样既保住了视觉层次，也不损失对比度。 -->
    <span
      v-if="count !== undefined"
      class="ml-0.5 rounded-[3px] bg-current/15 px-1 tabular-nums"
    >{{ count }}</span>
  </span>
</template>
