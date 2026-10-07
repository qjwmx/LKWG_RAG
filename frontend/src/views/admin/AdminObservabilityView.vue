<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { observabilityApi } from '@/api/admin'
import type {
  AgentUsage,
  CacheStatus,
  LlmBreakdown,
  ObservabilityOverview,
  RequestTraceRow,
} from '@/api/types'
import AdminLayout from '@/components/layout/AdminLayout.vue'

/**
 * 可观测性面板。**仅管理员可见**——入口只在管理后台侧栏，
 * 而后端在 admin router 上挂了 require_admin（普通用户直接调接口会 403）。
 * 前端隐藏入口只是不显示，不是安全边界。
 */
const overview = ref<ObservabilityOverview | null>(null)
const llm = ref<LlmBreakdown | null>(null)
const cache = ref<CacheStatus | null>(null)
const requests = ref<RequestTraceRow[]>([])
const requestTotal = ref(0)

const loading = ref(true)
const error = ref('')
const hours = ref(24)
const statusFilter = ref('')
const routeFilter = ref('')
const autoRefresh = ref(false)

let timer: number | null = null

/** 顶部指标卡。用 computed 而不是在模板里写六块重复结构。 */
const cards = computed(() => {
  const o = overview.value
  if (!o) return []
  return [
    { label: `请求数（${o.window_hours}h）`, value: String(o.requests), hint: `${o.qpm} 次/分钟` },
    {
      label: '错误率',
      value: `${(o.error_rate * 100).toFixed(1)}%`,
      hint: `${o.errors} 个 4xx/5xx`,
      cls: o.error_rate > 0.05 ? 'ink-error' : '',
    },
    { label: 'P95 耗时', value: `${o.latency.p95_ms} ms`, hint: `平均 ${o.latency.avg_ms} ms` },
    { label: 'Token 合计', value: o.total_tokens.toLocaleString(), hint: `${o.llm_calls} 次模型调用` },
    { label: 'P50 耗时', value: `${o.latency.p50_ms} ms`, hint: `P99 ${o.latency.p99_ms} ms` },
    {
      label: '每请求平均 Token',
      value: String(o.avg_tokens_per_request),
      hint: `样本 ${o.latency.sample_size} 条`,
    },
  ]
})

/** Agent 用量条形图。纯 CSS 宽度，不引图表库。 */
const agentBars = computed(() => {
  const agents = llm.value?.agents ?? []
  const max = Math.max(1, ...agents.map((a) => a.total_tokens))
  return agents.map((a: AgentUsage) => ({
    ...a,
    // 条长按"占最大值的比例"算，而不是占比——占比在小样本下会几乎看不见
    width: `${Math.round((a.total_tokens / max) * 100)}%`,
    share: `${(a.token_share * 100).toFixed(1)}%`,
  }))
})

const statusClass = (status: number): string => {
  if (status >= 500) return 'tint-error ink-error'
  if (status >= 400) return 'tint-warning ink-warning'
  return 'tint-success ink-success'
}

async function load(): Promise<void> {
  error.value = ''
  try {
    const [o, l, c, r] = await Promise.all([
      observabilityApi.overview(hours.value),
      observabilityApi.llm(hours.value),
      observabilityApi.cache(),
      observabilityApi.requests({
        limit: 60,
        status: statusFilter.value || undefined,
        route: routeFilter.value.trim() || undefined,
      }),
    ])
    overview.value = o
    llm.value = l
    cache.value = c
    requests.value = r.requests
    requestTotal.value = r.total
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loading.value = false
  }
}

async function reload(): Promise<void> {
  loading.value = true
  await load()
}

/** 自动刷新。页面不可见时不打接口——后台标签页不该持续请求。 */
function toggleAutoRefresh(): void {
  autoRefresh.value = !autoRefresh.value
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
  if (autoRefresh.value) {
    timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load()
    }, 10_000)
  }
}

onMounted(load)

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<template>
  <AdminLayout>
    <div class="mb-4 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 class="text-xl font-semibold">观测</h1>
        <p class="text-xs ink-muted">
          请求轨迹、模型用量与缓存状态。<b>仅管理员可见。</b>
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <select v-model.number="hours" class="select select-sm w-28" @change="reload">
          <option :value="1">近 1 小时</option>
          <option :value="6">近 6 小时</option>
          <option :value="24">近 24 小时</option>
          <option :value="168">近 7 天</option>
        </select>
        <button
          type="button"
          class="btn btn-sm"
          :class="autoRefresh ? 'btn-primary' : 'btn-ghost'"
          @click="toggleAutoRefresh"
        >
          {{ autoRefresh ? '自动刷新中' : '自动刷新' }}
        </button>
        <button type="button" class="btn btn-ghost btn-sm" @click="reload">刷新</button>
      </div>
    </div>

    <div v-if="error" class="alert alert-error mb-3 py-2 text-sm">{{ error }}</div>

    <div v-if="loading" class="flex justify-center py-16">
      <span class="loading loading-spinner loading-lg" />
    </div>

    <template v-else>
      <!-- 顶部指标卡 -->
      <div class="mb-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div v-for="card in cards" :key="card.label" class="surface p-4">
          <p class="text-xs ink-muted">{{ card.label }}</p>
          <p class="mt-1 text-2xl font-semibold tabular-nums" :class="card.cls">
            {{ card.value }}
          </p>
          <p class="mt-1 text-[11px] ink-subtle">{{ card.hint }}</p>
        </div>
      </div>

      <!-- 隐私声明：把"没采集什么"明确摆出来，避免被误解为埋点偷数据 -->
      <div v-if="overview" class="surface mb-4 p-3">
        <p class="text-[11px] ink-subtle">{{ overview.notice }}</p>
      </div>

      <!-- 写入器状态：队列满会丢轨迹，必须显式暴露 -->
      <div v-if="overview" class="surface mb-4 p-4">
        <h2 class="mb-2 text-sm font-semibold">采集器状态</h2>
        <div class="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs ink-muted">
          <span>
            运行中：
            <b :class="overview.writer.running ? 'ink-success' : 'ink-error'">
              {{ overview.writer.running ? '是' : '否' }}
            </b>
          </span>
          <span>队列：{{ overview.writer.queue_size }} / {{ overview.writer.queue_capacity }}</span>
          <span>已落库：{{ overview.writer.written }}</span>
          <span :class="overview.writer.dropped ? 'ink-warning' : ''">
            丢弃：{{ overview.writer.dropped }}
          </span>
          <span v-if="overview.usage_missing_calls" class="ink-warning">
            上游未返回用量的调用：{{ overview.usage_missing_calls }}
          </span>
        </div>
        <p class="mt-2 text-[11px] ink-subtle">
          队列满时轨迹会被<b>丢弃</b>而不是阻塞请求——观测数据不该影响业务。
          「丢弃」不为 0 说明采集跟不上，可以调大 OBSERVABILITY_QUEUE_SIZE。
        </p>
      </div>

      <!-- Agent 用量 -->
      <div class="surface mb-4 p-4">
        <h2 class="mb-3 text-sm font-semibold">
          模型用量（按 Agent 分组，近 {{ llm?.window_hours ?? hours }} 小时）
        </h2>
        <div v-if="!agentBars.length" class="py-6 text-center text-xs ink-subtle">
          还没有模型调用记录。跑一次对话或攻略研究就会出现。
        </div>
        <div v-else class="space-y-2.5">
          <div v-for="a in agentBars" :key="a.agent">
            <div class="mb-1 flex items-baseline justify-between gap-2 text-xs">
              <span class="font-medium">{{ a.agent }}</span>
              <span class="ink-muted tabular-nums">
                {{ a.total_tokens.toLocaleString() }} tokens · {{ a.calls }} 次 ·
                平均 {{ a.avg_duration_ms }} ms
              </span>
            </div>
            <div class="bg-base-content/8 h-2 w-full overflow-hidden rounded-full">
              <div class="bg-primary h-full rounded-full transition-all" :style="{ width: a.width }" />
            </div>
            <p class="mt-0.5 text-[10px] ink-subtle">占总用量 {{ a.share }}</p>
          </div>
        </div>
      </div>

      <!-- 最慢路由 -->
      <div v-if="overview?.slowest_routes?.length" class="surface mb-4 p-4">
        <h2 class="mb-3 text-sm font-semibold">最慢路由（样本 ≥ 3 次）</h2>
        <div class="space-y-1.5">
          <div
            v-for="r in overview.slowest_routes"
            :key="r.route"
            class="flex items-baseline justify-between gap-3 text-xs"
          >
            <code class="truncate font-mono text-[11px]">{{ r.route }}</code>
            <span class="shrink-0 ink-muted tabular-nums">
              平均 {{ r.avg_ms }} ms · 峰值 {{ r.max_ms }} ms · {{ r.calls }} 次
            </span>
          </div>
        </div>
      </div>

      <!-- 缓存状态 -->
      <div v-if="cache" class="surface mb-4 p-4">
        <h2 class="mb-2 text-sm font-semibold">缓存</h2>
        <div class="flex flex-wrap items-center gap-x-6 gap-y-1 text-xs ink-muted">
          <span>
            后端：
            <b>{{ cache.configured ? 'Redis' : '进程内' }}</b>
          </span>
          <span :class="cache.ok ? 'ink-success' : 'ink-warning'">
            {{ cache.ok ? '正常' : '降级' }}
          </span>
          <span v-if="cache.stats.keys !== undefined">键数：{{ cache.stats.keys }}</span>
          <span v-if="cache.stats.corpus_version !== undefined">
            语料版本：{{ cache.stats.corpus_version }}
          </span>
        </div>
        <p class="mt-1.5 text-[11px] ink-subtle">{{ cache.detail }}</p>
      </div>

      <!-- 请求轨迹 -->
      <div class="surface overflow-hidden">
        <div class="flex flex-wrap items-center gap-2 border-b p-3" style="border-color: var(--hairline)">
          <h2 class="text-sm font-semibold">请求轨迹</h2>
          <span class="text-[11px] ink-subtle">共 {{ requestTotal }} 条</span>
          <div class="ml-auto flex flex-wrap items-center gap-2">
            <input
              v-model="routeFilter"
              type="text"
              placeholder="按路由过滤"
              class="input input-sm w-40"
              @keyup.enter="reload"
            />
            <select v-model="statusFilter" class="select select-sm w-28" @change="reload">
              <option value="">全部状态</option>
              <option value="2xx">2xx</option>
              <option value="4xx">4xx</option>
              <option value="5xx">5xx</option>
            </select>
          </div>
        </div>

        <div v-if="!requests.length" class="py-12 text-center text-xs ink-subtle">
          还没有请求记录。
        </div>

        <div v-else class="scrollbar-slim overflow-x-auto">
          <table class="table table-sm">
            <thead>
              <tr class="text-[11px] ink-muted">
                <th>状态</th>
                <th>方法</th>
                <th>路由</th>
                <th class="text-right">耗时</th>
                <th class="text-right">Token</th>
                <th>用户</th>
                <th>时间</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in requests" :key="row.id" class="text-xs">
                <td>
                  <span class="rounded px-1.5 py-px text-[10px] tabular-nums" :class="statusClass(row.status)">
                    {{ row.status || '—' }}
                  </span>
                </td>
                <td class="font-mono text-[11px]">{{ row.method }}</td>
                <td>
                  <code class="font-mono text-[11px]">{{ row.route }}</code>
                  <p v-if="row.error" class="mt-0.5 text-[10px] ink-error">{{ row.error }}</p>
                </td>
                <td class="text-right tabular-nums">{{ row.duration_ms }} ms</td>
                <td class="text-right tabular-nums">
                  {{ row.total_tokens || '—' }}
                  <span v-if="row.llm_calls" class="ink-subtle">({{ row.llm_calls }})</span>
                </td>
                <td class="ink-muted">{{ row.username || '—' }}</td>
                <td class="whitespace-nowrap ink-subtle">{{ row.created_at }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </AdminLayout>
</template>
