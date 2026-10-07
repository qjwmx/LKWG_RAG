<script setup lang="ts">
import { computed, ref } from 'vue'

const props = defineProps<{
  categories: string[]
  supportedTypes: string[]
  defaultVersion: string
  maxMb: number
  isAdmin: boolean
  busy: boolean
  progress: number
}>()

const emit = defineEmits<{
  (
    e: 'submit',
    payload: { files: File[]; category: string; version: string },
  ): void
}>()

const files = ref<File[]>([])
const category = ref(props.categories[0] || '未分类')
const version = ref(props.defaultVersion)
const dragging = ref(false)
const localError = ref('')

/** accept 用 .ext 形式。后端按扩展名校验，这里保持一致，
 *  让用户在选文件阶段就被挡住，而不是上传完才失败。 */
const accept = computed(() => props.supportedTypes.map((t) => `.${t}`).join(','))

function addFiles(incoming: FileList | null): void {
  if (!incoming) return
  localError.value = ''
  const next = [...files.value]

  for (const file of Array.from(incoming)) {
    const ext = file.name.split('.').pop()?.toLowerCase() || ''
    if (!props.supportedTypes.includes(ext)) {
      localError.value = `${file.name}：不支持的类型 .${ext}`
      continue
    }
    if (file.size > props.maxMb * 1024 * 1024) {
      localError.value = `${file.name}：超过 ${props.maxMb}MB 上限`
      continue
    }
    // 同名同大小视为重复选择，避免用户反复拖同一份
    if (next.some((f) => f.name === file.name && f.size === file.size)) continue
    next.push(file)
  }
  files.value = next
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  addFiles(event.dataTransfer?.files ?? null)
}

function removeFile(index: number): void {
  files.value = files.value.filter((_, i) => i !== index)
}

function submit(): void {
  if (!files.value.length) return
  emit('submit', {
    files: files.value,
    category: category.value,
    version: version.value.trim() || props.defaultVersion,
  })
}

function clear(): void {
  files.value = []
  localError.value = ''
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

defineExpose({ clear })
</script>

<template>
  <div class="surface p-4">
    <h3 class="mb-1 text-sm font-semibold">上传文档</h3>
    <p class="mb-3 text-xs ink-muted">
      支持 {{ supportedTypes.join(' / ') }}，单个文件不超过 {{ maxMb }}MB。
      <template v-if="isAdmin">你是管理员，上传的文档进<b>公共知识库</b>，所有登录用户都能检索到。</template>
      <template v-else>上传的文档进<b>你的私有知识库</b>，只有你自己能检索到。</template>
    </p>

    <!-- 拖拽区：细虚线 + 悬停浅底 -->
    <div
      class="flex flex-col items-center justify-center rounded-xl border border-dashed p-6 transition-colors"
      :class="dragging ? 'border-primary bg-primary/5' : 'hover:bg-base-content/[0.03]'"
      style="border-color: var(--hairline-strong)"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="dragging = false"
      @drop.prevent="onDrop"
    >
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="mb-2 h-7 w-7 opacity-35">
        <path d="M9 16h6v-6h4l-7-7-7 7h4v6zm-4 2h14v2H5v-2z" />
      </svg>
      <p class="text-sm ink-muted">把文件拖到这里，或</p>
      <label class="btn btn-primary btn-sm mt-2">
        选择文件
        <input
          type="file"
          multiple
          class="hidden"
          :accept="accept"
          @change="(e) => { addFiles((e.target as HTMLInputElement).files); (e.target as HTMLInputElement).value = '' }"
        />
      </label>
    </div>

    <div v-if="localError" class="alert alert-warning mt-3 py-2 text-xs">{{ localError }}</div>

    <!-- 待上传列表 -->
    <ul v-if="files.length" class="mt-3 space-y-1">
      <li
        v-for="(file, index) in files"
        :key="`${file.name}-${index}`"
        class="bg-base-content/5 flex items-center gap-2 rounded-lg px-2 py-1.5 text-xs"
      >
        <span class="min-w-0 flex-1 truncate">{{ file.name }}</span>
        <span class="shrink-0 ink-muted">{{ formatSize(file.size) }}</span>
        <button
          type="button"
          class="btn btn-ghost btn-xs"
          :disabled="busy"
          @click="removeFile(index)"
        >
          ✕
        </button>
      </li>
    </ul>

    <!-- 入库参数 -->
    <div class="mt-3 grid gap-2 sm:grid-cols-2">
      <label class="form-control">
        <span class="mb-1 text-xs ink-muted">文档分类</span>
        <select v-model="category" class="select select-sm" :disabled="busy">
          <option v-for="item in categories" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label class="form-control">
        <span class="mb-1 text-xs ink-muted">文档版本</span>
        <input v-model="version" class="input input-sm" :disabled="busy" />
      </label>
    </div>

    <!-- 进度条：细线风格 -->
    <div v-if="busy" class="bg-base-content/10 mt-3 h-1 w-full overflow-hidden rounded-full">
      <div
        class="bg-primary h-full transition-[width] duration-300"
        :style="{ width: `${progress}%` }"
      />
    </div>

    <div class="mt-3 flex justify-end gap-2">
      <button type="button" class="btn btn-ghost btn-sm" :disabled="busy || !files.length" @click="clear">
        清空
      </button>
      <button
        type="button"
        class="btn btn-primary btn-sm"
        :disabled="busy || !files.length"
        @click="submit"
      >
        <span v-if="busy" class="loading loading-spinner loading-xs" />
        上传 {{ files.length ? `(${files.length})` : '' }}
      </button>
    </div>
  </div>
</template>
