import { get, post, upload } from './http'
import type { ChunksResult, DocumentRecord, UploadResult } from './types'

export const knowledgeApi = {
  list: (category?: string) =>
    get<DocumentRecord[]>('/knowledge_base/list_files', category ? { category } : undefined),

  detail: (document_id: string) => get<DocumentRecord>('/knowledge_base/detail', { document_id }),

  /** 取某文档的全部切片。引用高亮靠它：拿到 chunk_index 后滚动并高亮。 */
  chunks: (document_id: string) => get<ChunksResult>('/knowledge_base/chunks', { document_id }),

  upload: (files: File[], category: string, version: string, onProgress?: (p: number) => void) => {
    const form = new FormData()
    for (const file of files) {
      // 字段名必须是 files（后端 list[UploadFile] = File(...)），
      // 且同名多次 append 才能形成多文件
      form.append('files', file)
    }
    form.append('category', category)
    form.append('version', version)
    // 单文件时才传 title，后端多文件会忽略它
    if (files.length === 1) {
      form.append('title', files[0].name.replace(/\.[^.]+$/, ''))
    }
    return upload<UploadResult>('/knowledge_base/upload_docs', form, onProgress)
  },

  remove: (document_id: string) =>
    post<{ message: string }>('/knowledge_base/delete_docs', { document_id }),

  seed: () => post<UploadResult>('/knowledge_base/seed'),
}
