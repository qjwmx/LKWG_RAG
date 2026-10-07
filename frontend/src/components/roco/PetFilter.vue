<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRocoStore } from '@/stores/roco'
import AttributeChip from './AttributeChip.vue'

const roco = useRocoStore()
const {
  petKeyword,
  petAttributes,
  petMinTotal,
  petSort,
  attributeCounts,
  petsTotal,
  petsLoading,
} = storeToRefs(roco)

const keyword = ref('')
let debounce: number | null = null

onMounted(() => {
  if (!attributeCounts.value.length) roco.loadPets()
})

/** 输入防抖：465 只精灵，每敲一个字就请求一次既慢又吵。 */
watch(keyword, (value) => {
  if (debounce !== null) window.clearTimeout(debounce)
  debounce = window.setTimeout(() => roco.loadPets(value.trim()), 300)
})

const sortOptions = [
  { value: 'no', label: '图鉴编号' },
  { value: 'total_desc', label: '种族值 ↓' },
  { value: 'total_asc', label: '种族值 ↑' },
  { value: 'speed_desc', label: '速度 ↓' },
  { value: 'name', label: '名称' },
]

const totalOptions = [
  { value: 0, label: '不限' },
  { value: 500, label: '≥500' },
  { value: 600, label: '≥600' },
  { value: 700, label: '≥700' },
]

const hasFilters = computed(
  () => petAttributes.value.length > 0 || petMinTotal.value > 0 || !!petKeyword.value,
)

function clearAll(): void {
  keyword.value = ''
  roco.clearPetFilters()
}
</script>

<template>
  <div class="flex flex-col gap-3">
    <!-- 搜索：去掉 input-bordered，改用细边框，与整体极简风一致 -->
    <label class="input input-sm flex items-center gap-2">
      <svg
        xmlns="http://www.w3.org/2000/svg"
        viewBox="0 0 24 24"
        fill="currentColor"
        class="h-4 w-4 opacity-40"
      >
        <path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0016 9.5 6.5 6.5 0 109.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z" />
      </svg>
      <input v-model="keyword" type="search" class="grow" placeholder="搜索精灵名…" />
      <button
        v-if="keyword"
        type="button"
        class="btn btn-ghost btn-xs btn-circle"
        @click="keyword = ''"
      >
        ✕
      </button>
    </label>

    <!-- 属性筛选：每个属性用**自己的颜色**，选中态实心 -->
    <div>
      <div class="mb-1.5 flex items-center justify-between">
        <span class="text-xs font-medium ink-muted">属性筛选</span>
        <span class="text-[10px] ink-subtle">可多选（任一命中）</span>
      </div>
      <div class="flex flex-wrap gap-1">
        <button
          v-for="item in attributeCounts"
          :key="item.type"
          type="button"
          class="cursor-pointer transition-opacity hover:opacity-100"
          :class="petAttributes.includes(item.type) ? '' : 'ink-muted'"
          :title="`${item.type}：${item.count} 只`"
          @click="roco.toggleAttribute(item.type)"
        >
          <AttributeChip
            :type="item.type"
            :count="item.count"
            :active="petAttributes.includes(item.type)"
          />
        </button>
      </div>
    </div>

    <!-- 种族值与排序 -->
    <div class="grid grid-cols-2 gap-2">
      <label class="form-control">
        <span class="mb-1 text-[10px] ink-muted">种族值总和</span>
        <select
          :value="petMinTotal"
          class="select select-sm"
          @change="roco.setPetMinTotal(Number(($event.target as HTMLSelectElement).value))"
        >
          <option v-for="opt in totalOptions" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </option>
        </select>
      </label>
      <label class="form-control">
        <span class="mb-1 text-[10px] ink-muted">排序</span>
        <select
          :value="petSort"
          class="select select-sm"
          @change="roco.setPetSort(($event.target as HTMLSelectElement).value)"
        >
          <option v-for="opt in sortOptions" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </option>
        </select>
      </label>
    </div>

    <!-- 状态行 -->
    <div class="flex items-center justify-between text-[11px]">
      <span class="ink-muted">
        {{ petsTotal }} 只
        <template v-if="hasFilters">（已筛选）</template>
        <template v-if="petsLoading"> · 加载中…</template>
      </span>
      <button v-if="hasFilters" type="button" class="btn btn-ghost btn-xs" @click="clearAll">
        清除筛选
      </button>
    </div>
  </div>
</template>
