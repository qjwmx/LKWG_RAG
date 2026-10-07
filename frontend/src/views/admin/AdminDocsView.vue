<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useKnowledgeStore } from '@/stores/knowledge'
import type { DocumentRecord, SourceDoc } from '@/api/types'
import AdminLayout from '@/components/layout/AdminLayout.vue'
import DocumentTable from '@/components/knowledge/DocumentTable.vue'
import ChunkViewer from '@/components/knowledge/ChunkViewer.vue'

const knowledge = useKnowledgeStore()
const { documents, loading } = storeToRefs(knowledge)

const notice = ref('')
const errorMsg = ref('')
const activeDocument = ref<DocumentRecord | null>(null)

/** 指标组。用 computed 而不是在模板里重复 filter，避免每次渲染都遍历三遍。 */
const metrics = computed(() => [
  { label: '全部文档', value: documents.value.length },
  { label: '公共库', value: documents.value.filter((d) => d.scope === 'public').length },
  { label: '私有库', value: documents.value.filter((d) => d.scope === 'private').length },
])

onMounted(async () => {
  await knowledge.loadDocuments().catch(() => undefined)
})

async function onRemove(documentId: string): Promise<void> {
  const doc = documents.value.find((d) => d.id === documentId)
  const label = doc?.title || documentId
  const owner = doc?.owner ? `（归属 ${doc.owner}）` : ''
  if (
    !window.confirm(
      `确定删除《${label}》${owner} 吗？\n\n原始文件与向量索引会一并移除，历史问答记录保留。`,
    )
  ) {
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
  try {
    const result = await knowledge.seed()
    const ok = (result.uploaded || []).filter((r) => r.status === 'success').length
    const dup = (result.uploaded || []).filter((r) => r.status === 'duplicate').length
    notice.value = `示例导入完成：新增 ${ok} 份，跳过重复 ${dup} 份。`
  } catch (exc) {
    errorMsg.value = exc instanceof Error ? exc.message : String(exc)
  }
}

function asSource(document: DocumentRecord): SourceDoc {
  return {
    document_id: document.id,
    chunk_index: 0,
    title: document.title,
    category: document.category,
    version: document.version,
    file_name: document.file_name,
    score: 0,
    snippet: '',
  }
}
</script>

<template>
  <AdminLayout>
    <div class="mb-4 flex flex-wrap items-center justify-between gap-2">
      <div>
        <h1 class="text-xl font-semibold">文档管理</h1>
        <p class="text-xs ink-muted">
          这里列出<b>所有用户</b>上传的文档，包括他人的私有文档。作为 root 你可以删除任何一份。
        </p>
      </div>
      <button type="button" class="btn btn-outline btn-sm" @click="onSeed">导入示例文档</button>
    </div>

    <div v-if="notice" class="alert alert-success mb-3 py-2 text-sm">{{ notice }}</div>
    <div v-if="errorMsg" class="alert alert-error mb-3 py-2 text-sm">{{ errorMsg }}</div>

    <div class="mb-4 grid grid-cols-3 gap-3">
      <div v-for="m in metrics" :key="m.label" class="surface px-4 py-3">
        <div class="text-[11px] ink-subtle">{{ m.label }}</div>
        <div class="mt-0.5 text-2xl font-semibold tabular-nums">{{ m.value }}</div>
      </div>
    </div>

    <div class="surface p-4">
      <DocumentTable
        :documents="documents"
        :loading="loading"
        :is-admin="true"
        current-user=""
        :show-owner="true"
        @remove="onRemove"
        @view="(doc) => (activeDocument = doc)"
        @refresh="knowledge.loadDocuments()"
      />
    </div>

    <ChunkViewer
      v-if="activeDocument"
      :source="asSource(activeDocument)"
      @close="activeDocument = null"
    />
  </AdminLayout>
</template>
