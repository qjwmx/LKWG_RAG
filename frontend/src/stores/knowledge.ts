import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { knowledgeApi, systemApi } from '@/api'
import type { CategoriesResult, DocumentRecord } from '@/api/types'

export const useKnowledgeStore = defineStore('knowledge', () => {
  const documents = ref<DocumentRecord[]>([])
  const categories = ref<CategoriesResult | null>(null)
  const loading = ref(false)
  const error = ref('')

  const successCount = computed(() => documents.value.filter((d) => d.status === 'success').length)
  const totalChunks = computed(() =>
    documents.value.reduce((sum, d) => sum + (d.chunk_count || 0), 0),
  )

  async function loadCategories(): Promise<void> {
    if (categories.value) return
    // 分类与支持的文件类型来自 /system/categories（与后端 config 同源），
    // 不是 knowledge 路由下的接口。
    categories.value = await systemApi.categories()
  }

  async function loadDocuments(category?: string): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      documents.value = await knowledgeApi.list(category)
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
      throw exc
    } finally {
      loading.value = false
    }
  }

  async function upload(
    files: File[],
    category: string,
    version: string,
    onProgress?: (percent: number) => void,
  ) {
    const result = await knowledgeApi.upload(files, category, version, onProgress)
    // 上传后刷新列表，让新文档立刻出现（含 failed 的，用户要看得到失败原因）
    await loadDocuments()
    return result
  }

  async function remove(documentId: string): Promise<void> {
    await knowledgeApi.remove(documentId)
    documents.value = documents.value.filter((d) => d.id !== documentId)
  }

  async function seed() {
    const result = await knowledgeApi.seed()
    await loadDocuments()
    return result
  }

  return {
    documents,
    categories,
    loading,
    error,
    successCount,
    totalChunks,
    loadCategories,
    loadDocuments,
    upload,
    remove,
    seed,
  }
})
