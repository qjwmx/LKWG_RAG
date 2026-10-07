<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRocoStore } from '@/stores/roco'
import type { Pet } from '@/api/roco-types'
import { statBarClass, statTier } from './visual'
import PetAvatar from './PetAvatar.vue'
import AttributeChip from './AttributeChip.vue'

const emit = defineEmits<{ (e: 'pick', pet: Pet): void }>()

const roco = useRocoStore()
const { pets, petsLoading, selectedPet, petsTotal } = storeToRefs(roco)

const keyword = ref('')
let debounce: number | null = null

onMounted(() => {
  if (!pets.value.length) roco.loadPets()
})

watch(keyword, (value) => {
  if (debounce !== null) window.clearTimeout(debounce)
  debounce = window.setTimeout(() => roco.loadPets(value.trim()), 300)
})

/** 已选精灵的详情（含六维与技能），用于顶部展开区 */
const detail = computed(() => selectedPet.value)

function pick(pet: Pet): void {
  roco.selectPet(pet.name)
  emit('pick', pet)
}

function total(pet: Pet): number {
  return Number(pet.stats?.total || 0)
}

/** 把六维归一化成 0-1，用于画小条形图。
 *  用 160 作为上限——实测种族值单项最高在 150 左右，取 160 留余量，
 *  这样条形图的长度能反映真实差距，不会全部顶满。 */
const STAT_MAX = 160
function ratio(value: number): number {
  return Math.max(0.02, Math.min(1, Number(value || 0) / STAT_MAX))
}

const quickStats = [
  { key: 'hp', label: 'HP' },
  { key: 'atk', label: '物攻' },
  { key: 'sp_atk', label: '魔攻' },
  { key: 'def', label: '物防' },
  { key: 'sp_def', label: '魔防' },
  { key: 'spd', label: '速度' },
]

/** 骨架屏占位数量：与真实网格列数一致，避免加载完成时布局跳动。 */
const SKELETON_COUNT = 9
</script>

<template>
  <div class="flex h-full flex-col gap-2">
    <!-- 已选精灵详情：展示六维条形图，比纯数字直观 -->
    <div v-if="detail" class="surface rise-in p-3">
      <div class="flex items-start gap-3">
        <PetAvatar
          :name="detail.name"
          :src="detail.image_url"
          :attribute="detail.attributes[0]"
          size="lg"
        />
        <div class="min-w-0 flex-1">
          <div class="flex flex-wrap items-center gap-1.5">
            <span class="truncate font-semibold">{{ detail.name }}</span>
            <span class="text-[11px] ink-subtle">#{{ detail.no }}</span>
          </div>
          <div class="mt-1 flex flex-wrap gap-1">
            <AttributeChip v-for="attr in detail.attributes" :key="attr" :type="attr" />
          </div>
          <div class="mt-1 text-[11px] ink-muted">
            种族值总和 <b class="text-sm">{{ total(detail) }}</b>
            <span :class="statTier(total(detail)).cls"> · {{ statTier(total(detail)).label }}</span>
          </div>
          <div v-if="detail.ability?.name" class="mt-0.5 truncate text-[11px]">
            <span class="ink-muted">特性：</span>{{ detail.ability.name }}
          </div>
        </div>
      </div>

      <!-- 六维条形图：按数值分档着色（中性色阶，不做红绿评级观感） -->
      <div class="mt-3 space-y-1">
        <div v-for="stat in quickStats" :key="stat.key" class="flex items-center gap-2">
          <span class="w-8 shrink-0 text-[10px] ink-muted">{{ stat.label }}</span>
          <div class="bg-base-content/10 h-1.5 flex-1 overflow-hidden rounded-full">
            <div
              class="h-full rounded-full transition-all duration-500"
              :class="statBarClass(Number(detail.stats?.[stat.key] || 0))"
              :style="{ width: `${ratio(detail.stats?.[stat.key]) * 100}%` }"
            />
          </div>
          <span class="w-7 shrink-0 text-right text-[10px] tabular-nums">
            {{ detail.stats?.[stat.key] ?? '-' }}
          </span>
        </div>
      </div>
    </div>

    <!-- 精灵网格 -->
    <div class="scrollbar-slim min-h-0 flex-1 overflow-y-auto">
      <!-- 骨架屏：取代 spinner。形状与真实卡片一致，加载完成时不跳动 -->
      <div v-if="petsLoading && !pets.length" class="grid grid-cols-3 gap-2">
        <div v-for="n in SKELETON_COUNT" :key="n" class="surface flex flex-col items-center gap-1 p-2">
          <div class="skeleton h-14 w-14 rounded-full" />
          <div class="skeleton h-2.5 w-12" />
          <div class="skeleton h-2 w-8" />
        </div>
      </div>

      <div v-else-if="!pets.length" class="py-10 text-center text-xs ink-muted">
        没有符合条件的精灵，试试放宽筛选。
      </div>

      <div v-else class="grid grid-cols-3 gap-2">
        <button
          v-for="(pet, i) in pets"
          :key="pet.id"
          type="button"
          class="surface surface-interactive rise-in group flex flex-col items-center gap-0.5 p-2 text-center"
          :class="detail?.name === pet.name ? 'border-primary/60 bg-primary/5' : ''"
          :style="{ '--i': String(i) }"
          :title="pet.name"
          @click="pick(pet)"
        >
          <PetAvatar
            :name="pet.name"
            :src="pet.image_url"
            :attribute="pet.attributes[0]"
            size="md"
            class="transition-transform duration-200 group-hover:scale-110"
          />
          <span class="mt-1 w-full truncate text-[11px] font-medium">{{ pet.name }}</span>
          <span class="flex flex-wrap justify-center gap-0.5">
            <AttributeChip
              v-for="attr in pet.attributes.slice(0, 2)"
              :key="attr"
              :type="attr"
              size="xs"
            />
          </span>
          <span class="text-[10px] tabular-nums ink-subtle">{{ total(pet) }}</span>
        </button>
      </div>

      <!-- 只展示前 N 只时给出提示，避免用户以为"只有这些" -->
      <p
        v-if="pets.length && petsTotal > pets.length"
        class="py-3 text-center text-[10px] ink-subtle"
      >
        显示前 {{ pets.length }} 只（共 {{ petsTotal }} 只）—— 用搜索或筛选缩小范围
      </p>
    </div>
  </div>
</template>
