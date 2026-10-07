<script setup lang="ts">
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { AGENT_META, AGENT_ORDER, useRocoStore } from '@/stores/roco'

const roco = useRocoStore()
const { agents, researching } = storeToRefs(roco)

const steps = computed(() =>
  AGENT_ORDER.map((name) => ({
    name,
    meta: AGENT_META[name],
    runtime: agents.value[name],
  })),
)

/** 状态点：用 daisyUI 的 status 组件（自带内阴影，比纯色圆点精致）。 */
function statusClass(state: string): string {
  if (state === 'done') return 'status-success'
  if (state === 'running') return 'status-primary animate-pulse'
  if (state === 'failed') return 'status-warning'
  return ''
}

function stateLabel(state: string): string {
  if (state === 'done') return '完成'
  if (state === 'running') return '进行中'
  if (state === 'failed') return '无结果'
  return '等待'
}
</script>

<template>
  <div class="surface p-3">
    <div class="mb-3 flex items-center justify-between">
      <h3 class="text-sm font-semibold">研究流水线</h3>
      <!-- 研究进行中给标题加 aura 辉光：daisyUI 5.7 内置，
           比"转圈图标"更克制，同时明确表达"正在工作" -->
      <span v-if="researching" class="aura aura-glow aura-xs text-primary">
        <span class="loading loading-dots loading-xs" />
      </span>
    </div>

    <ol class="space-y-2.5">
      <li v-for="step in steps" :key="step.name" class="flex gap-2">
        <!-- 状态点 + 竖线 -->
        <div class="flex flex-col items-center pt-1">
          <span class="status status-sm" :class="statusClass(step.runtime.state)" />
        </div>

        <div class="min-w-0 flex-1 pb-1">
          <div class="flex items-center gap-2">
            <span class="text-[13px] font-medium">{{ step.meta.label }}</span>
            <!-- idle 状态原本是 opacity-35（实测 2.46:1，本来就不达标）。
                 它是**要读的状态文字**（"待机"/"等待"），不是装饰，
                 所以必须抬到 ink-subtle。与运行中的区别仍由字体粗细表达。 -->
            <span
              class="ml-auto text-[10px]"
              :class="step.runtime.state === 'idle' ? 'ink-subtle' : 'ink-muted'"
            >
              {{ stateLabel(step.runtime.state) }}
            </span>
          </div>
          <p class="mt-0.5 text-[11px] ink-subtle">
            {{ step.runtime.detail || step.meta.desc }}
          </p>

          <!-- Researcher 是并行的：按单元分列，否则多单元进度会互相覆盖 -->
          <ul v-if="step.runtime.units.length" class="mt-1 space-y-0.5">
            <li
              v-for="unit in step.runtime.units"
              :key="unit.unit_id"
              class="flex items-center gap-1.5 text-[10px] ink-muted"
            >
              <span
                class="status status-xs"
                :class="{
                  'status-success': unit.state === 'done',
                  'status-primary animate-pulse': unit.state === 'running',
                  'status-warning': unit.state === 'failed',
                }"
              />
              <span class="truncate">{{ unit.detail || unit.unit_id }}</span>
            </li>
          </ul>

          <!-- 细进度条：取代 daisyUI progress，与整体的"细线"语言一致 -->
          <div
            v-if="step.runtime.state === 'running'"
            class="bg-base-content/10 mt-1.5 h-0.5 w-full overflow-hidden rounded-full"
          >
            <div
              class="bg-primary h-full transition-[width] duration-500"
              :style="{ width: `${step.runtime.percent}%` }"
            />
          </div>
        </div>
      </li>
    </ol>
  </div>
</template>
