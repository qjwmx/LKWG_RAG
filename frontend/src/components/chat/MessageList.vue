<script setup lang="ts">
import { ref, watch } from 'vue'
import type { ChatMessage, SourceDoc } from '@/api/types'
import { useAutoScroll } from '@/composables/useAutoScroll'
import ChatMessageItem from './ChatMessage.vue'

const props = defineProps<{
  messages: ChatMessage[]
  streaming: boolean
  userName: string
}>()

const emit = defineEmits<{
  (e: 'open-source', source: SourceDoc): void
  (e: 'feedback', payload: { message: ChatMessage; rating: 'helpful' | 'needs_improvement' }): void
  (e: 'ask', question: string): void
}>()

const container = ref<HTMLElement | null>(null)
// 用消息数组本身作为滚动触发源：长度变化（新消息）与内容变化（流式追加）都会触发
const { showJumpButton, scrollToBottom, attach } = useAutoScroll(container, ref(props.messages))

watch(container, (el) => {
  if (el) attach()
})

// 推荐问题**必须能被库里的资料回答**。
// 这三份机制文档（属性克制 / 性格与个体值 / PvP 基础规则）是随项目提供的，
// 所以下面每个问题都对应其中一段——点了就有答案，不会一上来就"资料不足"。
const starters = [
  '双属性精灵的克制倍率怎么算？',
  '机械系为什么被叫做肉盾属性？',
  '物攻手应该选什么性格？',
  '一套 PvP 阵容通常需要哪几种角色？',
]
</script>

<template>
  <div class="relative flex-1 overflow-hidden">
    <div ref="container" class="scrollbar-slim h-full overflow-y-auto px-3 py-4 sm:px-4">
      <div class="mx-auto max-w-4xl">
        <!-- 空态：欢迎语 + 推荐问题。
             ★ 包在 .surface 卡片里，而不是直接放在内容区背景上。
             内容区的 .glass-region 只有 28%/40%（为了壁纸可见），
             裸文字压在上面最弱一档只有 3.8:1 / 2.0:1。
             文字必须落在卡片（55%/86%）上才达标。 -->
        <div v-if="!messages.length" class="surface mt-8 flex flex-col items-center justify-center px-4 py-12">
          <div
            class="bg-primary text-primary-content mb-4 flex h-14 w-14 items-center justify-center rounded-2xl text-2xl font-bold"
          >
            洛
          </div>
          <h2 class="text-xl font-semibold">洛克王国机制问答</h2>
          <p class="mt-1 text-sm ink-muted">
            {{ userName ? `${userName}，` : '' }}基于已入库文档回答，并标注引用来源
          </p>
          <p class="mt-1 text-xs ink-subtle">
            想按精灵查阵容、或让五个 Agent 做深度攻略研究，请到「阵容与攻略」
          </p>

          <div class="mt-8 grid w-full max-w-2xl gap-2 sm:grid-cols-2">
            <button
              v-for="(starter, i) in starters"
              :key="starter"
              type="button"
              class="surface surface-interactive rise-in hover:bg-base-content/5 h-auto px-3 py-2.5 text-left text-sm font-normal"
              :style="{ '--i': String(i) }"
              @click="emit('ask', starter)"
            >
              {{ starter }}
            </button>
          </div>
        </div>

        <ChatMessageItem
          v-for="message in messages"
          :key="message.id"
          :message="message"
          @open-source="(source) => emit('open-source', source)"
          @feedback="(payload) => emit('feedback', payload)"
        />
      </div>
    </div>

    <!-- 用户往上翻时出现"回到底部"，而不是把他拽回去 -->
    <Transition
      enter-active-class="transition duration-150"
      enter-from-class="opacity-0 translate-y-2"
      leave-active-class="transition duration-150"
      leave-to-class="opacity-0 translate-y-2"
    >
      <button
        v-if="showJumpButton"
        type="button"
        class="btn btn-circle btn-sm border-base-300 bg-base-100 absolute bottom-4 left-1/2 -translate-x-1/2 shadow-md"
        title="回到底部"
        @click="scrollToBottom(true)"
      >
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
          <path d="M12 16l-6-6h12l-6 6z" />
        </svg>
      </button>
    </Transition>
  </div>
</template>
