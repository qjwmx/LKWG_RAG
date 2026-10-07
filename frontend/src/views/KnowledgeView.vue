<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useKnowledgeStore } from '@/stores/knowledge'
import { useAuthStore } from '@/stores/auth'
import type { DocumentRecord, UploadedItem } from '@/api/types'
import AppShell from '@/components/layout/AppShell.vue'
import UploadDropzone from '@/components/knowledge/UploadDropzone.vue'
import DocumentTable from '@/components/knowledge/DocumentTable.vue'
import ChunkViewer from '@/components/knowledge/ChunkViewer.vue'

const knowledge = useKnowledgeStore()
const auth = useAuthStore()
const { documents, categories, loading } = storeToRefs(knowledge)

const busy = ref(false)
const progress = ref(0)
const results = ref<UploadedItem[]>([])
const failed = ref<Record<string, string>>({})
const notice = ref('')
const errorMsg = ref('')
const activeDocument = ref<DocumentRecord | null>(null)
const dropzone = ref<InstanceType<typeof UploadDropzone> | null>(null)

const supportedTypes = computed(() => categories.value?.supported_file_types ?? [])
const defaultVersion = computed(() => categories.value?.default_version ?? 'v1.0')
const maxMb = computed(() => categories.value?.upload_max_mb ?? 50)

/** 指标组。用 computed 而不是在模板里写三个块，
 *  这样加指标只改一处。 */
const metrics = computed(() => [
  { label: '可见文档', value: documents.value.length },
  { label: '已入库', value: knowledge.successCount },
  { label: '切片总数', value: knowledge.totalChunks },
])

onMounted(async () => {
  await Promise.all([
    knowledge.loadCategories().catch(() => undefined),
    knowledge.loadDocuments().catch(() => undefined),
  ])
})

async function onUpload(payload: {
  files: File[]
  category: string
  version: string
}): Promise<void> {
  busy.value = true
  progress.value = 0
  results.value = []
  failed.value = {}
  notice.value = ''
  errorMsg.value = ''

  try {
    const result = await knowledge.upload(
      payload.files,
      payload.category,
      payload.version,
      (percent) => (progress.value = percent),
    )
    results.value = result.uploaded || []
    failed.value = result.failed_files || {}
    dropzone.value?.clear()

    const ok = results.value.filter((r) => r.status === 'success').length
    const dup = results.value.filter((r) => r.status === 'duplicate').length
    notice.value = `完成：新增 ${ok} 份，跳过重复 ${dup} 份${
      Object.keys(failed.value).length ? `，失败 ${Object.keys(failed.value).length} 份` : ''
    }。`
  } catch (exc) {
    errorMsg.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
    progress.value = 0
  }
}

async function onRemove(documentId: string): Promise<void> {
  const doc = documents.value.find((d) => d.id === documentId)
  const label = doc?.title || documentId
  if (!window.confirm(`确定删除《${label}》吗？\n\n原始文件与向量索引会一并移除，历史问答记录保留。`)) {
    return
  }
  try {
    await knowledge.remove(documentId)
    notice.value = `已删除《${label}》。`
  } catch (exc) {
    errorMsg.value = exc instanceof Error ? exc.message : String(exc)
  }
}

async function onSeed(): Promise<void> {
  if (!window.confirm('导入 6 份示例制度文档到公共知识库？')) return
  busy.value = true
  try {
    const result = await knowledge.seed()
    results.value = result.uploaded || []
    failed.value = result.failed_files || {}
    notice.value = '示例文档已导入公共知识库。'
  } catch (exc) {
    errorMsg.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

/** 打开切片查看器。DocumentTable 传的是文档，ChunkViewer 需要 SourceDoc 形状，
 *  这里把 chunk_index 置 0（不带具体引用时从第一个切片看起）。 */
function onViewDocument(document: DocumentRecord): void {
  activeDocument.value = document
}
</script>

<template>
  <AppShell>
    <!-- 内容区：glass-region 现在是**透明的**（壁纸原样显示）。
         所以这里所有文字都必须落在 .surface 卡片上，不能裸放。 -->
    <div
      class="glass-region scrollbar-slim overflow-y-auto"
      style="height: calc(100vh - var(--shell-header-h))"
    >
      <div class="mx-auto max-w-6xl space-y-4 p-4">
        <!-- 页头包在卡片里：它是**裸文字**（标题 + 说明），
             直接压在壁纸上时说明文字只有 4.22:1，低于 4.5。
             包一层 .surface 后底色变实，与其它页一致。 -->
        <div class="surface flex flex-wrap items-center justify-between gap-2 px-4 py-3">
          <div>
            <h1 class="text-xl font-semibold">知识库</h1>
            <p class="text-xs ink-muted">
              <template v-if="auth.isAdmin">
                你是管理员（root）：这里的列表包含<b>所有用户</b>的文档，你也可以删除任何一份。
              </template>
              <template v-else>
                这里显示公共知识库与你上传的私有文档。
              </template>
            </p>
          </div>
          <div class="flex items-center gap-2">
            <button
              v-if="auth.isAdmin"
              type="button"
              class="btn btn-outline btn-sm"
              :disabled="busy"
              @click="onSeed"
            >
              导入示例文档
            </button>
          </div>
        </div>

        <!-- 指标组：极简数字卡，去掉 daisyUI stats 的大留白 -->
        <div class="grid grid-cols-3 gap-3">
          <div v-for="metric in metrics" :key="metric.label" class="surface px-4 py-3">
            <div class="text-[11px] ink-subtle">{{ metric.label }}</div>
            <div class="mt-0.5 text-2xl font-semibold tabular-nums">{{ metric.value }}</div>
          </div>
        </div>

        <div v-if="notice" class="alert alert-success py-2 text-sm">
          <span>{{ notice }}</span>
        </div>
        <div v-if="errorMsg" class="alert alert-error py-2 text-sm">
          <span>{{ errorMsg }}</span>
        </div>

        <!-- 逐项上传结果：部分失败不阻断整批，所以逐行显示 -->
        <div v-if="results.length || Object.keys(failed).length" class="surface p-4">
          <h3 class="mb-2 text-sm font-semibold">本次导入结果</h3>
          <ul class="space-y-1 text-xs">
            <li v-for="item in results" :key="item.file_name" class="flex items-start gap-2">
              <span
                class="shrink-0 rounded px-1.5 py-px text-[11px]"
                :class="{
                  'tint-success ink-success': item.status === 'success',
                  'tint-warning ink-warning': item.status === 'duplicate',
                  'tint-error ink-error': item.status === 'failed',
                }"
              >
                {{ item.status === 'success' ? '已入库' : item.status === 'duplicate' ? '已存在' : '失败' }}
              </span>
              <span class="min-w-0 flex-1">
                <b>{{ item.file_name }}</b> — {{ item.message }}
              </span>
            </li>
            <li
              v-for="(message, name) in failed"
              :key="`failed-${name}`"
              class="flex items-start gap-2"
            >
              <span class="shrink-0 rounded tint-error px-1.5 py-px text-[11px] ink-error">失败</span>
              <span class="min-w-0 flex-1"><b>{{ name }}</b> — {{ message }}</span>
            </li>
          </ul>
        </div>

        <UploadDropzone
          ref="dropzone"
          :categories="categories?.categories || []"
          :supported-types="supportedTypes"
          :default-version="defaultVersion"
          :max-mb="maxMb"
          :is-admin="auth.isAdmin"
          :busy="busy"
          :progress="progress"
          @submit="onUpload"
        />

        <div class="surface p-4">
          <h3 class="mb-3 text-sm font-semibold">文档列表</h3>
          <DocumentTable
            :documents="documents"
            :loading="loading"
            :is-admin="auth.isAdmin"
            :current-user="auth.user?.username || ''"
            :show-owner="auth.isAdmin"
            @remove="onRemove"
            @view="onViewDocument"
            @refresh="knowledge.loadDocuments()"
          />
        </div>
      </div>
    </div>

    <ChunkViewer
      v-if="activeDocument"
      :source="{
        document_id: activeDocument.id,
        chunk_index: 0,
        title: activeDocument.title,
        category: activeDocument.category,
        version: activeDocument.version,
        file_name: activeDocument.file_name,
        score: 0,
        snippet: '',
      }"
      @close="activeDocument = null"
    />
  </AppShell>
</template>
