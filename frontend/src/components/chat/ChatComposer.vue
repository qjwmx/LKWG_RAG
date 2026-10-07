<script setup lang="ts">
import { ref } from 'vue'

const props = defineProps<{
  disabled?: boolean
  streaming?: boolean
}>()

const emit = defineEmits<{
  (e: 'send', question: string): void
  (e: 'stop'): void
}>()

const text = ref('')
const textarea = ref<HTMLTextAreaElement | null>(null)

function submit(): void {
  const value = text.value.trim()
  if (!value || props.disabled) return
  emit('send', value)
  text.value = ''
  resize()
}

/** Enter 发送，Shift+Enter 换行。这是聊天输入框的通用约定；
 *  输入法组合期间不能触发发送（否则中文选词会误发）。 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter') return
  if (event.shiftKey) return
  if (event.isComposing) return
  event.preventDefault()
  submit()
}

/** 输入框随内容长高，但不超过 200px（超过就内部滚动） */
function resize(): void {
  const el = textarea.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${Math.min(el.scrollHeight, 200)}px`
}
</script>

<template>
  <!-- 输入区：半透明底而不是 bg-base-100。
       它坐在 ChatView 的 .glass-region 上，所以不需要自己再做模糊
       （底下的模糊已经做过了），只叠一层更实的填充把输入框衬托出来。 -->
  <div class="bg-base-100/50 border-t px-3 py-3 sm:px-4" style="border-color: var(--hairline)">
    <div class="mx-auto flex max-w-4xl items-end gap-2">
      <textarea
        ref="textarea"
        v-model="text"
        rows="1"
        class="textarea scrollbar-slim max-h-[200px] flex-1 resize-none leading-relaxed"
        placeholder="问机制，例如：双属性精灵的克制倍率怎么算？（Enter 发送，Shift+Enter 换行）"
        :disabled="disabled"
        @keydown="onKeydown"
        @input="resize"
      />

      <button
        v-if="streaming"
        type="button"
        class="btn btn-error btn-square"
        title="停止生成"
        @click="emit('stop')"
      >
        <span class="block h-3 w-3 rounded-[2px] bg-current" />
      </button>
      <button
        v-else
        type="button"
        class="btn btn-primary btn-square"
        :disabled="disabled || !text.trim()"
        title="发送"
        @click="submit"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 24 24"
          fill="currentColor"
          class="h-5 w-5"
        >
          <path d="M3.4 20.4l17.45-7.48a1 1 0 000-1.84L3.4 3.6a.993.993 0 00-1.39.91L2 9.12c0 .5.37.93.87.99L17 12 2.87 13.88c-.5.07-.87.5-.87 1l.01 4.61c0 .71.73 1.2 1.39.91z" />
        </svg>
      </button>
    </div>
  </div>
</template>
