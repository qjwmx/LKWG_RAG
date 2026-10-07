<script setup lang="ts">
import { computed, ref } from 'vue'
import { attributeStyle, initialOf } from './visual'

const props = withDefaults(
  defineProps<{
    name: string
    src?: string
    attribute?: string
  }>(),
  { src: '', attribute: '' },
)

/** 加载失败退化到属性色占位块。
 *  图标是热链，断网/被删都会走到这里，不能显示裂图。 */
const failed = ref(false)
const showImage = computed(() => !!props.src && !failed.value)

/** 颜色用 CSS 变量，不用内联 hex：色板调整时只改 style.css 一处。 */
const vars = computed(() => attributeStyle(props.attribute || ''))
</script>

<template>
  <div
    class="bg-base-content/5 flex h-8 w-8 shrink-0 items-center justify-center overflow-hidden rounded-md"
    :style="vars"
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
    <span
      v-else
      class="text-[10px] font-semibold"
      :style="{ color: 'color-mix(in oklch, var(--attr) 60%, var(--color-base-content))' }"
    >
      {{ initialOf(name) }}
    </span>
  </div>
</template>
