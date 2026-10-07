<script setup lang="ts">
import { computed } from 'vue'
import { renderMarkdown } from '@/composables/useMarkdown'

const props = defineProps<{
  content: string
  streaming?: boolean
}>()

// 流式期间也用同一套渲染：useMarkdown 会处理未闭合的代码围栏，
// 所以这里不需要为"半截内容"单独写一条渲染路径。
const html = computed(() => renderMarkdown(props.content))
</script>

<template>
  <div
    class="chat-prose prose prose-sm max-w-none dark:prose-invert"
    :class="{ 'streaming-cursor': streaming }"
    v-html="html"
  />
</template>
