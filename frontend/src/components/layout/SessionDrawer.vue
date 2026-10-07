<script setup lang="ts">
import { computed } from 'vue'
import type { SessionSummary } from '@/api/types'

const props = defineProps<{
  sessions: SessionSummary[]
  currentId: string
  loading: boolean
  isAdmin: boolean
}>()

const emit = defineEmits<{
  (e: 'select', id: string): void
  (e: 'create'): void
  (e: 'remove', id: string): void
}>()

/** 会话按"今天 / 近 7 天 / 更早"分组——几十条会话平铺时很难找。 */
const grouped = computed(() => {
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()
  const sevenDaysAgo = startOfToday - 6 * 24 * 3600 * 1000

  const buckets: Array<{ label: string; items: SessionSummary[] }> = [
    { label: '今天', items: [] },
    { label: '近 7 天', items: [] },
    { label: '更早', items: [] },
  ]

  for (const session of props.sessions) {
    const raw = session.last_at || session.started_at
    const time = raw ? new Date(raw.replace(' ', 'T')).getTime() : 0
    if (!time || Number.isNaN(time)) buckets[2].items.push(session)
    else if (time >= startOfToday) buckets[0].items.push(session)
    else if (time >= sevenDaysAgo) buckets[1].items.push(session)
    else buckets[2].items.push(session)
  }

  return buckets.filter((bucket) => bucket.items.length > 0)
})

function title(session: SessionSummary): string {
  return session.question || '（无标题会话）'
}
</script>

<template>
  <!-- 侧栏是"内容会从旁边滚过去"的结构层，用 glass-region。
       原来只是一层极淡的 bg-base-content/[0.03]，几乎看不出底色——
       放在壁纸之上会变成"文字直接压在壁纸上"，可读性没有保障。 -->
  <aside class="glass-region flex h-full w-72 flex-col">
    <div class="p-3">
      <button type="button" class="btn btn-primary btn-block btn-sm" @click="emit('create')">
        + 新对话
      </button>
    </div>

    <div class="scrollbar-slim flex-1 overflow-y-auto px-2 pb-3">
      <!-- 骨架屏：会话列表的形状固定，用骨架比 spinner 更少跳动 -->
      <div v-if="loading && !sessions.length" class="space-y-1.5 px-1 py-2">
        <div v-for="n in 6" :key="n" class="skeleton h-10 w-full" />
      </div>

      <p v-else-if="!sessions.length" class="px-2 py-6 text-center text-xs ink-muted">
        还没有历史会话。<br />提问后会自动保存。
      </p>

      <template v-else>
        <div v-for="bucket in grouped" :key="bucket.label" class="mb-3">
          <div class="px-2 py-1 text-[11px] font-medium tracking-wide uppercase ink-subtle">
            {{ bucket.label }}
          </div>
          <ul class="flex flex-col gap-0.5">
            <li v-for="session in bucket.items" :key="session.session_id">
              <div
                class="group hover:bg-base-content/5 relative flex items-center gap-1 rounded-lg transition-colors"
                :class="session.session_id === currentId ? 'bg-base-content/8' : ''"
              >
                <span
                  v-if="session.session_id === currentId"
                  class="bg-primary absolute top-1.5 bottom-1.5 left-0 w-0.5 rounded-full"
                />
                <button
                  type="button"
                  class="min-w-0 flex-1 px-2 py-1.5 text-left"
                  :title="title(session)"
                  @click="emit('select', session.session_id)"
                >
                  <span class="block truncate text-[13px]">{{ title(session) }}</span>
                  <span class="mt-0.5 flex items-center gap-1 text-[10px] ink-subtle">
                    <span>{{ session.turns }} 轮</span>
                    <span>·</span>
                    <span class="truncate">{{ session.category }}</span>
                    <!-- 管理员看得到全部会话，标出归属否则分不清是谁的 -->
                    <template v-if="isAdmin && session.username">
                      <span>·</span>
                      <span class="truncate">{{ session.username }}</span>
                    </template>
                  </span>
                </button>
                <button
                  type="button"
                  class="btn btn-ghost btn-xs mr-1 opacity-0 group-hover:opacity-100"
                  title="删除会话"
                  @click.stop="emit('remove', session.session_id)"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-3.5 w-3.5">
                    <path d="M6 19a2 2 0 002 2h8a2 2 0 002-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" />
                  </svg>
                </button>
              </div>
            </li>
          </ul>
        </div>
      </template>
    </div>
  </aside>
</template>
