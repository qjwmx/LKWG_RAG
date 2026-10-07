<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { knowledgeApi } from '@/api/knowledge'
import type { ChunkRecord, DocumentRecord, SourceDoc } from '@/api/types'

const props = defineProps<{ source: SourceDoc }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const document = ref<DocumentRecord | null>(null)
const chunks = ref<ChunkRecord[]>([])
const loading = ref(true)
const error = ref('')
const container = ref<HTMLElement | null>(null)

const targetIndex = computed(() => props.source.chunk_index)

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    const result = await knowledgeApi.chunks(props.source.document_id)
    document.value = result.document
    chunks.value = result.chunks
    await nextTick()
    scrollToTarget()
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loading.value = false
  }
}

/** 把目标切片滚到视野中并加高亮。
 *
 *  用 querySelector + scrollIntoView 而不是自己算 offsetTop：
 *  切片高度不一，手算偏移在长文档里会偏。 */
function scrollToTarget(): void {
  const el = container.value?.querySelector<HTMLElement>(`[data-chunk="${targetIndex.value}"]`)
  if (!el) return
  el.scrollIntoView({ block: 'center', behavior: 'smooth' })
  // 重新触发动画：先移除类，强制回流后再加回去
  el.classList.remove('chunk-highlight')
  void el.offsetWidth
  el.classList.add('chunk-highlight')
}

watch(() => props.source.document_id, load)
onMounted(load)
</script>

<template>
  <div class="modal modal-open" role="dialog">
    <!-- 弹窗浮在正文之上，用 glass-panel：它背后是变化的正文，
         只叠半透明色会"字透字"。daisyUI 的 .modal-box 自带不透明底色，
         靠 .glass-panel 覆盖它（实测确认覆盖生效，见 README）。 -->
    <div class="modal-box glass-panel rise-in flex h-[85vh] max-w-4xl flex-col p-0">
      <header
        class="flex items-start justify-between gap-3 border-b p-4"
        style="border-color: var(--hairline)"
      >
        <div class="min-w-0">
          <h3 class="truncate text-base font-semibold">
            {{ document?.title || source.title }}
          </h3>
          <div class="mt-1 flex flex-wrap items-center gap-1.5 text-xs ink-muted">
            <span class="bg-base-content/8 rounded px-1.5 py-px text-[11px]">
              {{ document?.category || source.category }}
            </span>
            <span class="bg-base-content/8 rounded px-1.5 py-px text-[11px]">
              {{ document?.version || source.version }}
            </span>
            <span class="truncate">{{ document?.file_name || source.file_name }}</span>
          </div>
        </div>
        <button type="button" class="btn btn-ghost btn-sm btn-square" @click="emit('close')">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
            <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
          </svg>
        </button>
      </header>

      <div ref="container" class="scrollbar-slim flex-1 overflow-y-auto p-4">
        <div v-if="loading" class="space-y-3 py-2">
          <div v-for="n in 4" :key="n" class="skeleton h-24 w-full" />
        </div>

        <div v-else-if="error" class="alert alert-error">
          <span>{{ error }}</span>
        </div>

        <template v-else>
          <p class="mb-3 text-xs ink-muted">
            共 {{ chunks.length }} 个切片。引用的是第
            <span class="text-primary font-medium">{{ targetIndex + 1 }}</span> 个（已高亮）。
          </p>

          <div class="space-y-3">
            <article
              v-for="chunk in chunks"
              :key="chunk.chunk_index"
              :data-chunk="chunk.chunk_index"
              class="surface p-3"
              :class="chunk.chunk_index === targetIndex ? 'border-primary/50 bg-primary/5' : ''"
            >
              <div class="mb-1.5 flex items-center gap-2">
                <span
                  class="rounded px-1.5 py-px text-[11px] tabular-nums"
                  :class="
                    chunk.chunk_index === targetIndex
                      ? 'tint-primary text-primary font-medium'
                      : 'tint-neutral'
                  "
                >
                  #{{ chunk.chunk_index + 1 }}
                </span>
                <span v-if="chunk.chunk_index === targetIndex" class="text-primary text-xs font-medium">
                  本次回答引用的片段
                </span>
              </div>
              <p class="text-sm leading-relaxed whitespace-pre-wrap">{{ chunk.content }}</p>
            </article>
          </div>
        </template>
      </div>

      <footer
        class="flex items-center justify-between border-t p-3"
        style="border-color: var(--hairline)"
      >
        <span class="text-xs ink-muted">
          相似度 {{ source.score.toFixed(4) }}
        </span>
        <button type="button" class="btn btn-sm" @click="emit('close')">关闭</button>
      </footer>
    </div>
    <div class="modal-backdrop bg-black/30" @click="emit('close')" />
  </div>
</template>
