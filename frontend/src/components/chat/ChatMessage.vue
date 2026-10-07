<script setup lang="ts">
import { computed } from 'vue'
import type { ChatMessage, SourceDoc } from '@/api/types'
import MarkdownBody from './MarkdownBody.vue'
import CitationList from './CitationList.vue'

const props = defineProps<{ message: ChatMessage }>()
const emit = defineEmits<{
  (e: 'open-source', source: SourceDoc): void
  (e: 'feedback', payload: { message: ChatMessage; rating: 'helpful' | 'needs_improvement' }): void
}>()

const isUser = computed(() => props.message.role === 'user')
const showActions = computed(
  () => !isUser.value && !!props.message.qa_log_id && !props.message.streaming,
)

/* 助手气泡用**无尾巴圆角块**，而不是 daisyUI 的 chat-bubble。
 *
 * 为什么换掉 chat-bubble：
 * 1. chat-bubble-neutral 的 --color-neutral 在**明暗两套主题下都是深色**，
 *    而 typography 插件的正文色在浅色主题里是深色 → 深色字压在深色气泡上，
 *    对比度接近 1:1。用户看到一整块黑色，会以为"没有返回答案"，
 *    而 store 里其实有完整内容、库里也已落库。
 * 2. 现代 AI 产品的助手回复通常是"无底色 + 左侧一条细线"，
 *    比一个实心气泡更轻，长文阅读也更舒服。
 *
 * 用户消息仍保留底色（与助手侧形成对比，一眼分清谁说的）。
 */
const bubbleClass = computed(() => {
  if (isUser.value) return 'bg-primary text-primary-content rounded-2xl rounded-br-md'
  if (props.message.error) return 'bg-error/8 rounded-2xl rounded-bl-md'
  // 助手：无底色，只留左侧一条细线
  return 'bg-base-content/[0.03] rounded-2xl rounded-bl-md'
})

const time = computed(() => {
  if (!props.message.created_at) return ''
  const date = new Date(props.message.created_at)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
})
</script>

<template>
  <!-- 用自定义布局而不是 daisyUI 的 chat 组件：
       chat 依赖 chat-start/chat-end + chat-bubble 三件套，
       而助手侧现在是无底色块，套进去反而要层层覆写。

       data-role 是给自动化探针用的稳定钩子：对比度检查要能精确定位
       "助手正文"与"用户消息"两个块，靠 class 名（Tailwind 原子类）选不稳。 -->
  <div
    class="rise-in flex gap-3 py-2.5"
    :class="isUser ? 'flex-row-reverse' : ''"
    :data-role="isUser ? 'user' : 'assistant'"
  >
    <div
      class="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold"
      :class="isUser ? 'bg-base-content/10' : 'bg-primary text-primary-content'"
    >
      {{ isUser ? '我' : 'AI' }}
    </div>

    <div class="min-w-0 flex-1" :class="isUser ? 'flex flex-col items-end' : ''">
      <div class="mb-1 flex items-center gap-2 text-[11px] ink-subtle">
        <span>{{ isUser ? '我' : '知识库助手' }}</span>
        <time v-if="time">{{ time }}</time>
        <span v-if="message.question_type" class="bg-base-content/8 rounded px-1.5 py-px">
          {{ message.question_type }}
        </span>
      </div>

      <div
        class="max-w-[min(46rem,88vw)] px-3.5 py-2.5"
        :class="bubbleClass"
        :style="
          !isUser && !message.error
            ? { borderLeft: '2px solid color-mix(in oklch, var(--color-primary) 45%, transparent)' }
            : undefined
        "
      >
        <!-- 用户消息是纯文本，不该走 Markdown 渲染：
             否则用户输入 # 或 * 会被当成语法，显示得和他打的字不一样 -->
        <p v-if="isUser" class="whitespace-pre-wrap break-words text-sm">{{ message.content }}</p>

        <template v-else>
          <div v-if="message.error" class="text-sm">
            <p class="font-medium ink-error">回答失败</p>
            <p class="mt-1 text-xs opacity-80">{{ message.error }}</p>
          </div>

          <MarkdownBody
            v-else
            :content="message.content"
            :streaming="message.streaming"
          />

          <!-- 流式刚开始、还没有任何内容时给个占位，避免气泡塌成一条线 -->
          <div
            v-if="message.streaming && !message.content"
            class="flex items-center gap-2 py-1 text-sm ink-muted"
          >
            <span class="loading loading-dots loading-sm" />
            <span>正在检索知识库…</span>
          </div>
        </template>
      </div>

      <div v-if="!isUser" class="mt-1.5 flex flex-wrap items-center gap-2">
        <CitationList
          v-if="message.source_docs?.length"
          :sources="message.source_docs"
          @open="(source) => emit('open-source', source)"
        />

        <!-- 联网失败/不可用：**用 info 而不是 error**。
             回答是好的，只是这次没联上网——渲染成"失败"会让用户
             以为整轮问答挂了，从而重问一遍。 -->
        <p v-if="message.web_error" class="w-full text-[11px] ink-info">
          {{ message.web_error }}
        </p>
      </div>

      <!-- 网络来源：**与本地引用分开一组**。
           两者可信度不同（本地是我们导入的、网络是外部未验证），
           混在一起用户就分不清哪条结论有本地数据支撑。 -->
      <div v-if="!isUser && message.web_sources?.length" class="mt-2 w-full">
        <div class="mb-1.5 flex flex-wrap items-baseline gap-1.5 text-xs font-medium ink-muted">
          <span>网络来源</span>
          <span class="text-[10px] font-normal ink-warning">外部内容 · 未经验证 · 仅供补充参考</span>
        </div>
        <ul class="grid gap-1.5 sm:grid-cols-2">
          <li v-for="source in message.web_sources" :key="source.url" class="surface p-2.5">
            <a
              :href="source.url"
              target="_blank"
              rel="noopener noreferrer nofollow"
              class="tint-primary text-primary text-[13px] font-medium hover:underline"
            >
              {{ source.title }}
            </a>
            <p class="mt-1 line-clamp-2 text-[11px] ink-muted">{{ source.snippet }}</p>
            <p class="mt-1 truncate text-[10px] ink-subtle">{{ source.url }}</p>
          </li>
        </ul>
      </div>

      <div v-if="!isUser && showActions" class="mt-1.5 flex flex-wrap items-center gap-2">
        <div class="flex items-center gap-1">
          <button
            type="button"
            class="btn btn-ghost btn-xs gap-1"
            :class="{ 'ink-success': message.feedback === 'helpful' }"
            :disabled="!!message.feedback"
            @click="emit('feedback', { message, rating: 'helpful' })"
          >
            有帮助
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-xs gap-1"
            :class="{ 'ink-warning': message.feedback === 'needs_improvement' }"
            :disabled="!!message.feedback"
            @click="emit('feedback', { message, rating: 'needs_improvement' })"
          >
            需改进
          </button>
          <span v-if="message.feedback" class="ml-1 text-[11px] ink-muted">已记录反馈</span>
        </div>
      </div>
    </div>
  </div>
</template>
