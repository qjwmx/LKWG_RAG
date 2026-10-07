<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { systemApi } from '@/api'
import type { HealthResult, Stats } from '@/api/types'
import AdminLayout from '@/components/layout/AdminLayout.vue'

const stats = ref<Stats | null>(null)
const health = ref<HealthResult | null>(null)
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    const [statsResult, healthResult] = await Promise.all([
      systemApi.stats(),
      systemApi.health(),
    ])
    stats.value = statsResult
    health.value = healthResult
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loading.value = false
  }
})

const cards = [
  { key: 'document_count', label: '文档数量', hint: '已入库的文档份数' },
  { key: 'category_count', label: '覆盖分类', hint: '至少有一份文档的分类数' },
  { key: 'qa_count', label: '问答记录', hint: '累计提问轮数' },
  { key: 'feedback_count', label: '反馈数量', hint: '有帮助 / 需改进的累计条数' },
] as const

/** 依赖状态列表。用 computed 而不是在模板里写三块重复结构。 */
const dependencies = computed(() => {
  const h = health.value
  if (!h) return []
  return [
    { label: '数据库', ok: h.postgres.ok, detail: h.postgres.detail, provider: '' },
    {
      label: '嵌入服务',
      ok: h.embedding.ok,
      detail: h.embedding.detail,
      provider: h.embedding.provider,
    },
    { label: '对话模型', ok: h.llm.ok, detail: h.llm.detail, provider: '' },
    {
      // Redis 是可选增强：没配时 ok=true（走进程内缓存，单进程部署足够）。
      // 只有"配了却连不上"才算异常，那时多实例的令牌吊销与登录限流
      // 会各自为政——这是个安全缺口，必须在界面上说清楚。
      label: 'Redis 缓存',
      ok: h.redis.ok,
      detail: h.redis.detail,
      provider: h.redis.configured ? 'redis' : '进程内',
    },
  ]
})
</script>

<template>
  <AdminLayout>
    <div class="mb-4">
      <h1 class="text-xl font-semibold">统计</h1>
      <p class="text-xs ink-muted">知识库整体情况与依赖健康状态。</p>
    </div>

    <div v-if="error" class="alert alert-error mb-3 py-2 text-sm">{{ error }}</div>

    <div v-if="loading" class="flex justify-center py-16">
      <span class="loading loading-spinner loading-lg" />
    </div>

    <template v-else>
      <div class="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div v-for="card in cards" :key="card.key" class="surface p-4">
          <p class="text-xs ink-muted">{{ card.label }}</p>
          <p class="mt-1 text-3xl font-semibold tabular-nums">
            {{ stats?.[card.key] ?? 0 }}
          </p>
          <p class="mt-1 text-[11px] ink-subtle">{{ card.hint }}</p>
        </div>
      </div>

      <!-- 用户统计：管理员专属 -->
      <div v-if="stats?.user_count !== undefined" class="mb-4 grid gap-3 sm:grid-cols-2">
        <div class="surface p-4">
          <p class="text-xs ink-muted">注册用户</p>
          <p class="mt-1 text-3xl font-semibold tabular-nums">{{ stats.user_count }}</p>
          <p class="mt-1 text-[11px] ink-subtle">不含 .env 预置的 root 账号</p>
        </div>
        <div class="surface p-4">
          <p class="text-xs ink-muted">待审批</p>
          <p
            class="mt-1 text-3xl font-semibold tabular-nums"
            :class="stats.pending_user_count ? 'ink-warning' : ''"
          >
            {{ stats.pending_user_count ?? 0 }}
          </p>
          <p class="mt-1 text-[11px] ink-subtle">需要管理员在「用户审批」里处理</p>
        </div>
      </div>

      <!-- 依赖健康：嵌入服务没起来是"检索不到"最常见的原因，必须显式暴露 -->
      <div class="surface p-4">
        <h2 class="mb-3 text-sm font-semibold">依赖状态</h2>
        <div v-if="health" class="space-y-2.5">
          <div v-for="dep in dependencies" :key="dep.label" class="flex items-start gap-3">
            <span
              class="mt-0.5 shrink-0 rounded px-1.5 py-px text-[11px]"
              :class="dep.ok ? 'tint-success ink-success' : 'tint-error ink-error'"
            >
              {{ dep.ok ? '正常' : '异常' }}
            </span>
            <div class="min-w-0">
              <p class="text-sm font-medium">
                {{ dep.label }}
                <span v-if="dep.provider" class="bg-base-content/8 ml-1 rounded px-1 text-[10px]">
                  {{ dep.provider }}
                </span>
              </p>
              <p class="text-xs break-words ink-muted">{{ dep.detail }}</p>
            </div>
          </div>
        </div>

        <div
          v-if="health && health.status === 'degraded'"
          class="tint-warning mt-3 rounded-lg px-3 py-2 text-xs ink-warning"
        >
          <span>
            当前处于降级状态：部分依赖不可用或使用了 hash 伪嵌入。
            伪嵌入下检索结果<b>没有语义意义</b>，请把 EMBEDDING_PROVIDER 改为 ollama 或 openai。
          </span>
        </div>
      </div>
    </template>
  </AdminLayout>
</template>
