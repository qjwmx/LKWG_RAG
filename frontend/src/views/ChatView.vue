<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useChatStore } from '@/stores/chat'
import { useAuthStore } from '@/stores/auth'
import { useKnowledgeStore } from '@/stores/knowledge'
import type { ChatMessage, SourceDoc } from '@/api/types'
import AppShell from '@/components/layout/AppShell.vue'
import SessionDrawer from '@/components/layout/SessionDrawer.vue'
import MessageList from '@/components/chat/MessageList.vue'
import ChatComposer from '@/components/chat/ChatComposer.vue'
import PipelineProgress from '@/components/chat/PipelineProgress.vue'
import WebToggle from '@/components/chat/WebToggle.vue'
import ChunkViewer from '@/components/knowledge/ChunkViewer.vue'

const chat = useChatStore()
const auth = useAuthStore()
const knowledge = useKnowledgeStore()

// storeToRefs 而不是解构：直接解构 store 会丢掉响应性
const {
  messages,
  sessions,
  sessionId,
  category,
  isStreaming,
  sessionsLoading,
  agents,
  useWeb,
  webAvailable,
  webStatus,
} = storeToRefs(chat)

const drawerOpen = ref(false)
const activeSource = ref<SourceDoc | null>(null)

onMounted(async () => {
  await Promise.all([
    chat.loadSessions(),
    knowledge.loadCategories().catch(() => undefined),
    // 联网可用性：决定「联网搜索」按钮能不能点。
    // 失败不报错——按钮显示不可用即可，不该打扰用户。
    chat.loadWebStatus(),
  ])
})

async function onSend(question: string): Promise<void> {
  await chat.send(question)
  // 新会话要出现在侧栏里，所以答完刷新一次列表
  await chat.loadSessions().catch(() => undefined)
}

async function onSelectSession(id: string): Promise<void> {
  await chat.openSession(id)
  drawerOpen.value = false
}

async function onNewSession(): Promise<void> {
  chat.newSession()
  drawerOpen.value = false
}

async function onRemoveSession(id: string): Promise<void> {
  await chat.deleteSession(id)
}

async function onFeedback(payload: {
  message: ChatMessage
  rating: 'helpful' | 'needs_improvement'
}): Promise<void> {
  try {
    await chat.submitFeedback(payload.message, payload.rating)
  } catch {
    // 反馈失败不打断对话，静默即可——它是可选操作
  }
}

function onOpenSource(source: SourceDoc): void {
  activeSource.value = source
}
</script>

<template>
  <AppShell>
    <!-- daisyUI drawer：侧栏与内容必须是同一个 drawer 下的兄弟节点，
         这是该组件最常见的踩坑点（放错层级会导致侧栏盖不住内容）。 -->
    <div class="drawer h-full">
      <input v-model="drawerOpen" type="checkbox" class="drawer-toggle" />

      <!-- drawer-content 是"内容区"这一结构层：用 glass-region，
           让消息列表压在磨砂玻璃上而不是直接压在壁纸上。 -->
      <div
        class="drawer-content glass-region flex flex-col"
        style="height: calc(100vh - var(--shell-header-h))"
      >
        <!-- 移动端才显示的开抽屉按钮 -->
        <div
          class="flex items-center gap-2 border-b px-3 py-2 lg:hidden"
          style="border-color: var(--hairline)"
        >
          <label for="session-drawer" class="btn btn-ghost btn-sm btn-square drawer-button">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
              <path d="M3 6h18v2H3V6zm0 5h18v2H3v-2zm0 5h18v2H3v-2z" />
            </svg>
          </label>
          <span class="text-sm ink-muted">历史会话</span>
        </div>

        <!-- 知识范围：只在选定的分类里检索。
             半透明底（原来是 bg-base-100 不透明，会把壁纸挡成一条白带）。 -->
        <div
          class="bg-base-100/40 flex flex-wrap items-center gap-2 border-b px-3 py-2"
          style="border-color: var(--hairline)"
        >
          <label class="flex shrink-0 items-center gap-2 text-xs">
            <!-- whitespace-nowrap：不加的话"知识范围"会在窄容器里
                 被拆成两行（"知识" / "范围"），看起来像排版坏了。 -->
            <span class="ink-muted whitespace-nowrap">知识范围</span>
            <select v-model="category" class="select select-xs">
              <option
                v-for="option in knowledge.categories?.filter_options || ['全部']"
                :key="option"
                :value="option"
              >
                {{ option }}
              </option>
            </select>
          </label>

          <!-- ★ 联网搜索开关：**高亮 = 本轮会联网，不亮 = 不联网**。
               放在输入区上方而不是藏在设置里——它按次计费，
               用户必须在每次提问前都能看见当前状态。 -->
          <WebToggle v-model="useWeb" :available="webAvailable" :disabled="isStreaming" />

          <span v-if="webStatus && !webAvailable" class="text-[11px] ink-subtle">
            在 backend/.env 里填 TAVILY_API_KEY 即可启用
          </span>
          <span v-else class="text-[11px] ink-subtle">
            选定分类后检索会更聚焦（过滤下推到数据库，不是检索完再筛）
          </span>
        </div>

        <!-- 五 Agent 检索进度：只在作答过程中出现 -->
        <PipelineProgress :agents="agents" :active="isStreaming" />

        <MessageList
          :messages="messages"
          :streaming="isStreaming"
          :user-name="auth.displayName"
          @open-source="onOpenSource"
          @feedback="onFeedback"
          @ask="onSend"
        />

        <ChatComposer
          :streaming="isStreaming"
          @send="onSend"
          @stop="chat.stopStreaming()"
        />
      </div>

      <!-- 侧栏：桌面端常驻，移动端作为抽屉 -->
      <div class="drawer-side z-30">
        <label for="session-drawer" class="drawer-overlay" @click="drawerOpen = false" />
        <SessionDrawer
          :sessions="sessions"
          :current-id="sessionId"
          :loading="sessionsLoading"
          :is-admin="auth.isAdmin"
          @select="onSelectSession"
          @create="onNewSession"
          @remove="onRemoveSession"
        />
      </div>
    </div>

    <ChunkViewer
      v-if="activeSource"
      :source="activeSource"
      @close="activeSource = null"
    />
  </AppShell>
</template>
