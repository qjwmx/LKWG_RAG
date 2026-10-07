<script setup lang="ts">
import { computed } from 'vue'
import { AGENT_META, AGENT_ORDER, type AgentRuntime } from '@/agents'
import type { AgentName } from '@/api/roco-types'

/** 对话页顶部的五 Agent 检索进度。
 *
 *  只在一轮问答进行中显示：答完后进度面板会让位给引用卡片，
 *  留着它只会占掉本就不多的垂直空间。
 *
 *  这是**如实反映后端在做什么**，不是装饰：用户能看到 Planner 拆了几个
 *  子问题、Researcher 并行查了几路，从而理解为什么这个回答值得信任
 *  （或为什么它说"资料不足"）。
 */

const props = defineProps<{
  agents: Record<AgentName, AgentRuntime>
  active: boolean
}>()

const steps = computed(() =>
  AGENT_ORDER.map((name) => ({
    name,
    meta: AGENT_META[name],
    runtime: props.agents[name],
  })),
)

/** 总进度：取已到达的最大百分比。
 *  用最大而不是平均——并行的 Researcher 会让平均值忽上忽下地跳。 */
const percent = computed(() =>
  Math.max(0, ...AGENT_ORDER.map((name) => props.agents[name].percent)),
)

/** 当前正在跑的步骤（用于显示 detail 文案）。 */
const running = computed(() => steps.value.find((s) => s.runtime.state === 'running'))

/** 状态点颜色。用 daisyUI 的 status 组件（自带内阴影，比纯色圆点精致）。 */
function statusClass(state: string): string {
  if (state === 'done') return 'status-success'
  if (state === 'running') return 'status-primary animate-pulse'
  if (state === 'failed') return 'status-warning'
  return ''
}
</script>

<template>
  <div
    v-if="active"
    class="bg-base-content/[0.03] border-b px-3 py-2"
    style="border-color: var(--hairline)"
  >
    <div class="mx-auto flex max-w-4xl items-center gap-3">
      <!-- 环形总进度：daisyUI 5.7 的 radial-progress，比细条更醒目 -->
      <div
        class="radial-progress text-primary shrink-0"
        :style="{ '--value': percent, '--size': '2.25rem', '--thickness': '3px' }"
        role="progressbar"
        :aria-valuenow="percent"
        aria-valuemin="0"
        aria-valuemax="100"
      >
        <span class="text-[9px] tabular-nums">{{ percent }}</span>
      </div>

      <div class="min-w-0 flex-1">
        <div class="flex items-baseline gap-2">
          <span class="text-[11px] font-medium">五 Agent 检索中</span>
          <span class="truncate text-[10px] ink-subtle">
            {{ running?.runtime.detail || running?.meta.desc || '' }}
          </span>
        </div>

        <!-- 五个阶段：状态点 + 名称 + 细进度条 -->
        <ol class="mt-1 flex items-start gap-2">
          <li
            v-for="step in steps"
            :key="step.name"
            class="min-w-0 flex-1"
            :title="step.runtime.detail || step.meta.desc"
          >
            <div class="flex items-center gap-1">
              <span
                class="status status-xs shrink-0"
                :class="statusClass(step.runtime.state)"
              />
              <!-- 同 AgentPipeline：idle 是要读的阶段名，
                   opacity-35（实测 2.46:1）本来就不达标，抬到 ink-subtle。
                   与"进行中"的区别仍由字体粗细表达。 -->
              <span
                class="truncate text-[10px]"
                :class="step.runtime.state === 'idle' ? 'ink-subtle' : 'font-medium'"
              >
                {{ step.meta.label }}
              </span>
            </div>
            <div class="bg-base-content/10 mt-1 h-0.5 w-full overflow-hidden rounded-full">
              <div
                class="bg-primary h-full transition-[width] duration-500"
                :style="{ width: `${step.runtime.percent}%` }"
              />
            </div>
          </li>
        </ol>
      </div>
    </div>
  </div>
</template>
