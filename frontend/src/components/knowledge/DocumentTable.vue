<script setup lang="ts">
import { computed, ref } from 'vue'
import type { DocumentRecord } from '@/api/types'

const props = defineProps<{
  documents: DocumentRecord[]
  loading: boolean
  isAdmin: boolean
  currentUser: string
  showOwner: boolean
}>()

const emit = defineEmits<{
  (e: 'remove', id: string): void
  (e: 'view', document: DocumentRecord): void
  (e: 'refresh'): void
}>()

const keyword = ref('')
const scopeFilter = ref<'all' | 'public' | 'private'>('all')
const statusFilter = ref<'all' | 'success' | 'processing' | 'failed'>('all')

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return props.documents.filter((doc) => {
    if (scopeFilter.value !== 'all' && doc.scope !== scopeFilter.value) return false
    if (statusFilter.value !== 'all' && doc.status !== statusFilter.value) return false
    if (!kw) return true
    return (
      doc.title.toLowerCase().includes(kw) ||
      doc.file_name.toLowerCase().includes(kw) ||
      doc.category.toLowerCase().includes(kw) ||
      doc.owner.toLowerCase().includes(kw)
    )
  })
})

/** 能否删除。**与后端 documents.can_manage 必须一致**——
 *  前端只负责不显示无权操作的按钮，真正的拦截在服务端。 */
function canManage(doc: DocumentRecord): boolean {
  if (props.isAdmin) return true
  return doc.scope === 'private' && doc.owner === props.currentUser
}

/** 状态标签：用浅底 + 同色文字，而不是 daisyUI 的实心 badge。
 *  实心 badge 在大表格里会形成一片色块，抢走对内容本身的注意力。 */
function statusBadge(doc: DocumentRecord): { cls: string; text: string } {
  if (doc.status === 'success') return { cls: 'tint-success ink-success', text: '已入库' }
  if (doc.status === 'processing') return { cls: 'tint-warning ink-warning', text: '处理中' }
  return { cls: 'tint-error ink-error', text: '失败' }
}

function formatSize(length: number): string {
  if (length < 1000) return `${length} 字`
  return `${(length / 1000).toFixed(1)}k 字`
}
</script>

<template>
  <div>
    <!-- 筛选栏 -->
    <div class="mb-3 flex flex-wrap items-center gap-2">
      <input
        v-model="keyword"
        type="search"
        class="input input-sm w-full sm:w-56"
        placeholder="搜索标题 / 文件名 / 分类"
      />
      <select v-model="scopeFilter" class="select select-sm">
        <option value="all">全部范围</option>
        <option value="public">公共库</option>
        <option value="private">私有库</option>
      </select>
      <select v-model="statusFilter" class="select select-sm">
        <option value="all">全部状态</option>
        <option value="success">已入库</option>
        <option value="processing">处理中</option>
        <option value="failed">失败</option>
      </select>
      <span class="text-xs ink-muted">
        共 {{ filtered.length }} / {{ documents.length }} 份
      </span>
      <button type="button" class="btn btn-ghost btn-sm ml-auto" @click="emit('refresh')">
        刷新
      </button>
    </div>

    <!-- 骨架屏 -->
    <div v-if="loading" class="space-y-2 py-2">
      <div v-for="n in 5" :key="n" class="skeleton h-10 w-full" />
    </div>

    <div v-else-if="!filtered.length" class="py-16 text-center ink-muted">
      <p>{{ documents.length ? '没有符合筛选条件的文档。' : '知识库还是空的，先上传一份文档吧。' }}</p>
    </div>

    <div v-else class="overflow-x-auto">
      <!-- 去掉 table-zebra：斑马纹与极简风冲突，
           改用极细下边框 + 行悬停浅底 -->
      <table class="table table-sm">
        <thead>
          <tr class="border-base-content/10 border-b">
            <th class="text-[11px] font-medium ink-muted">标题</th>
            <th class="text-[11px] font-medium ink-muted">分类</th>
            <th class="text-[11px] font-medium ink-muted">版本</th>
            <th v-if="showOwner" class="text-[11px] font-medium ink-muted">归属</th>
            <th class="text-[11px] font-medium ink-muted">范围</th>
            <th class="text-right text-[11px] font-medium ink-muted">切片</th>
            <th class="text-[11px] font-medium ink-muted">状态</th>
            <th class="text-right text-[11px] font-medium ink-muted">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="doc in filtered"
            :key="doc.id"
            class="border-base-content/5 hover:bg-base-content/[0.03] border-b transition-colors"
          >
            <td class="max-w-[16rem]">
              <div class="truncate text-[13px] font-medium" :title="doc.title">{{ doc.title }}</div>
              <div class="truncate text-[11px] ink-subtle" :title="doc.file_name">
                {{ doc.file_name }} · {{ doc.file_type }} · {{ formatSize(doc.text_length) }}
              </div>
            </td>
            <td>
              <span class="bg-base-content/8 rounded px-1.5 py-px text-[11px]">{{ doc.category }}</span>
            </td>
            <td class="text-xs ink-muted">{{ doc.version }}</td>
            <td v-if="showOwner" class="text-xs ink-muted">{{ doc.owner || '—' }}</td>
            <td>
              <span
                class="rounded px-1.5 py-px text-[11px]"
                :class="doc.scope === 'public' ? 'tint-info ink-info' : 'bg-base-content/8'"
              >
                {{ doc.scope === 'public' ? '公共' : '私有' }}
              </span>
            </td>
            <td class="text-right text-xs tabular-nums ink-muted">{{ doc.chunk_count }}</td>
            <td>
              <span class="rounded px-1.5 py-px text-[11px]" :class="statusBadge(doc).cls">
                {{ statusBadge(doc).text }}
              </span>
            </td>
            <td class="text-right">
              <div class="flex justify-end gap-1">
                <button
                  type="button"
                  class="btn btn-ghost btn-xs"
                  title="查看切片"
                  :disabled="doc.status !== 'success'"
                  @click="emit('view', doc)"
                >
                  查看
                </button>
                <button
                  v-if="canManage(doc)"
                  type="button"
                  class="btn btn-ghost btn-xs ink-error"
                  title="删除文档及其向量索引"
                  @click="emit('remove', doc.id)"
                >
                  删除
                </button>
              </div>
              <div
                v-if="doc.status === 'failed' && doc.error"
                class="mt-1 max-w-[14rem] text-left text-[10px] ink-error"
              >
                {{ doc.error }}
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
