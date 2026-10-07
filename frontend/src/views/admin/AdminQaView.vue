<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { adminApi } from '@/api/admin'
import type { FeedbackRecord, QaLogRecord } from '@/api/types'
import AdminLayout from '@/components/layout/AdminLayout.vue'

const logs = ref<QaLogRecord[]>([])
const feedback = ref<FeedbackRecord[]>([])
const summary = ref({ total: 0, helpful: 0, needs_improvement: 0 })
const loading = ref(true)
const error = ref('')
const keyword = ref('')
const tab = ref<'qa' | 'feedback'>('qa')

const filteredLogs = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return logs.value
  return logs.value.filter(
    (log) =>
      log.question.toLowerCase().includes(kw) ||
      log.username.toLowerCase().includes(kw) ||
      log.answer.toLowerCase().includes(kw),
  )
})

const satisfaction = computed(() => {
  const rated = summary.value.helpful + summary.value.needs_improvement
  if (!rated) return 0
  return Math.round((summary.value.helpful / rated) * 100)
})

/** 指标组。用 computed 而不是在模板里写五块重复结构。 */
const metrics = computed(() => [
  { label: '问答记录', value: logs.value.length, cls: '' },
  { label: '反馈总数', value: summary.value.total, cls: '' },
  { label: '有帮助', value: summary.value.helpful, cls: 'ink-success' },
  { label: '需改进', value: summary.value.needs_improvement, cls: 'ink-warning' },
  { label: '满意度', value: `${satisfaction.value}%`, cls: '' },
])

onMounted(async () => {
  try {
    const [qaResult, fbResult] = await Promise.all([adminApi.qaLogs(200), adminApi.feedback()])
    logs.value = qaResult
    feedback.value = fbResult.feedback
    summary.value = fbResult.summary
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loading.value = false
  }
})

function truncate(text: string, max = 120): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}
</script>

<template>
  <AdminLayout>
    <div class="mb-4">
      <h1 class="text-xl font-semibold">问答与反馈</h1>
      <p class="text-xs ink-muted">全部用户的问答记录与反馈（含所有私有文档相关的问答）。</p>
    </div>

    <div v-if="error" class="alert alert-error mb-3 py-2 text-sm">{{ error }}</div>

    <div class="mb-4 grid grid-cols-2 gap-3 sm:grid-cols-5">
      <div v-for="m in metrics" :key="m.label" class="surface px-3 py-2.5">
        <div class="text-[11px] ink-subtle">{{ m.label }}</div>
        <div class="mt-0.5 text-xl font-semibold tabular-nums" :class="m.cls">{{ m.value }}</div>
      </div>
    </div>

    <!-- 分段控件：与 StrategyView / AdminUsersView 同一套语言 -->
    <div class="bg-base-content/5 mb-3 inline-flex gap-0.5 rounded-lg p-0.5">
      <button
        type="button"
        class="rounded-md px-3 py-1 text-xs transition-colors"
        :class="tab === 'qa' ? 'tint-neutral font-medium' : 'ink-subtle hover:text-base-content'"
        @click="tab = 'qa'"
      >
        问答记录
      </button>
      <button
        type="button"
        class="rounded-md px-3 py-1 text-xs transition-colors"
        :class="tab === 'feedback' ? 'tint-neutral font-medium' : 'ink-subtle hover:text-base-content'"
        @click="tab = 'feedback'"
      >
        反馈记录
      </button>
    </div>

    <div v-if="loading" class="surface space-y-2 p-4">
      <div v-for="n in 5" :key="n" class="skeleton h-16 w-full" />
    </div>

    <template v-else-if="tab === 'qa'">
      <input
        v-model="keyword"
        type="search"
        class="input input-sm mb-3 w-full sm:w-72"
        placeholder="搜索问题 / 回答 / 用户名"
      />

      <div v-if="!filteredLogs.length" class="surface py-16 text-center">
        <p class="ink-muted">还没有问答记录。</p>
      </div>

      <div v-else class="space-y-2">
        <article v-for="log in filteredLogs" :key="log.id" class="surface p-3">
          <div class="mb-2 flex flex-wrap items-center gap-1.5 text-xs ink-muted">
            <span class="bg-base-content/8 rounded px-1.5 py-px text-[11px]">
              {{ log.username || '（无归属）' }}
            </span>
            <span class="bg-base-content/8 rounded px-1.5 py-px text-[11px]">{{ log.category }}</span>
            <span v-if="log.question_type" class="bg-base-content/8 rounded px-1.5 py-px text-[11px]">
              {{ log.question_type }}
            </span>
            <span class="ml-auto">{{ log.created_at }}</span>
          </div>
          <p class="text-sm font-medium">{{ log.question }}</p>
          <p class="mt-1.5 text-xs leading-relaxed whitespace-pre-wrap ink-muted">
            {{ truncate(log.answer, 400) }}
          </p>
          <p v-if="log.source_docs?.length" class="mt-2 text-[11px] ink-subtle">
            引用了 {{ log.source_docs.length }} 份文档：{{
              log.source_docs.map((s) => s.title).join('、')
            }}
          </p>
        </article>
      </div>
    </template>

    <template v-else>
      <div v-if="!feedback.length" class="surface py-16 text-center">
        <p class="ink-muted">还没有反馈记录。</p>
      </div>

      <div v-else class="surface overflow-x-auto">
        <table class="table table-sm">
          <thead>
            <tr class="border-base-content/10 border-b">
              <th class="text-[11px] font-medium ink-muted">评价</th>
              <th class="text-[11px] font-medium ink-muted">问答 ID</th>
              <th class="text-[11px] font-medium ink-muted">补充说明</th>
              <th class="text-[11px] font-medium ink-muted">提交时间</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in feedback"
              :key="item.id"
              class="border-base-content/5 hover:bg-base-content/[0.03] border-b transition-colors"
            >
              <td>
                <span
                  class="rounded px-1.5 py-px text-[11px]"
                  :class="
                    item.rating === 'helpful'
                      ? 'tint-success ink-success'
                      : 'tint-warning ink-warning'
                  "
                >
                  {{ item.rating === 'helpful' ? '有帮助' : '需改进' }}
                </span>
              </td>
              <td class="font-mono text-[11px] ink-muted">{{ item.qa_log_id }}</td>
              <td class="text-xs">{{ item.comment || '—' }}</td>
              <td class="text-xs ink-muted">{{ item.created_at }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </AdminLayout>
</template>
