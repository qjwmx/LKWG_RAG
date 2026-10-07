<script setup lang="ts">
import type { SourceDoc } from '@/api/types'

defineProps<{ sources: SourceDoc[] }>()

const emit = defineEmits<{ (e: 'open', source: SourceDoc): void }>()
</script>

<template>
  <div v-if="sources.length" class="w-full">
    <div class="mb-1.5 flex items-center gap-1.5 text-xs font-medium ink-muted">
      <span>引用文档</span>
      <span class="bg-base-content/8 rounded px-1 text-[10px] tabular-nums">
        {{ sources.length }}
      </span>
    </div>

    <div class="grid gap-2 sm:grid-cols-2">
      <button
        v-for="(source, index) in sources"
        :key="`${source.document_id}-${source.chunk_index}`"
        type="button"
        class="surface surface-interactive hover:bg-base-content/5 p-2.5 text-left"
        :title="`查看《${source.title}》第 ${source.chunk_index + 1} 个切片`"
        @click="emit('open', source)"
      >
        <div class="flex items-start gap-2">
          <span
            class="tint-primary text-primary mt-0.5 shrink-0 rounded px-1.5 text-[10px] font-medium tabular-nums"
          >
            {{ index + 1 }}
          </span>
          <div class="min-w-0 flex-1">
            <div class="truncate text-[13px] font-medium">{{ source.title }}</div>
            <div class="mt-0.5 flex flex-wrap items-center gap-1 text-[10px] ink-subtle">
              <span>{{ source.category }}</span>
              <span>·</span>
              <span>{{ source.version }}</span>
            </div>
            <p class="mt-1 line-clamp-2 text-[11px] ink-muted">{{ source.snippet }}</p>
            <div class="mt-1.5 flex items-center gap-2">
              <div class="bg-base-content/10 h-1 flex-1 overflow-hidden rounded-full">
                <div
                  class="bg-primary h-full rounded-full"
                  :style="{ width: `${Math.max(2, Math.min(100, source.score * 100))}%` }"
                />
              </div>
              <span class="text-[10px] tabular-nums ink-subtle">{{ source.score.toFixed(3) }}</span>
            </div>
          </div>
        </div>
      </button>
    </div>
  </div>
</template>

<style scoped>
/* Tailwind 4 自带 line-clamp，这里只是确保两行截断不会撑高卡片 */
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
