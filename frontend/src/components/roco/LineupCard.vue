<script setup lang="ts">
import { computed } from 'vue'
import type { Lineup } from '@/api/roco-types'
import { attributeStyle } from './visual'
import PetAvatar from './PetAvatar.vue'
import AttributeChip from './AttributeChip.vue'

const props = defineProps<{
  lineup: Lineup
  /** 高亮这套阵容里的哪只精灵（用户选中的那只） */
  highlight?: string
  /** 入场动画的错峰序号 */
  index?: number
}>()

const emit = defineEmits<{ (e: 'open', lineup: Lineup): void }>()

/** 成员头像：优先用后端返回的 member_images（含图片与属性），
 *  没有就退化到纯名字（老数据或接口未升级时）。 */
const members = computed(() => {
  const images = props.lineup.member_images || []
  if (images.length) return images
  return (props.lineup.member_names || []).map((name) => ({
    name,
    image_url: '',
    attributes: [] as string[],
  }))
})

/** 弱点：列表接口不带 type_analysis，只有详情接口才有。
 *  这里只显示已有的，不额外请求。 */
const weakTo = computed(() => props.lineup.type_analysis?.weak_to?.slice(0, 4) || [])

/** 高亮精灵的属性色：用它给卡片描边。
 *
 *  旧版用 `ring-warning`（黄色）——黄色在界面里是"警告"语义，
 *  用来表示"你选中的精灵"会让人以为这套阵容有问题。 */
const highlightStyle = computed(() => {
  const target = members.value.find((m) => m.name === props.highlight)
  const attr = target?.attributes?.[0]
  if (!attr) return undefined
  const base = attributeStyle(attr)
  return {
    ...base,
    borderColor: 'color-mix(in oklch, var(--attr) 55%, transparent)',
  }
})
</script>

<template>
  <article
    class="surface surface-interactive rise-in group cursor-pointer p-3"
    :style="index !== undefined ? { ...highlightStyle, '--i': String(index) } : highlightStyle"
    @click="emit('open', lineup)"
  >
    <!-- 标题行 -->
    <div class="flex items-start justify-between gap-2">
      <div class="min-w-0">
        <h4 class="truncate font-medium">{{ lineup.title }}</h4>
        <div class="mt-0.5 flex flex-wrap items-center gap-1.5 text-[11px] ink-subtle">
          <span
            class="rounded px-1 text-[10px] font-medium"
            :class="lineup.lineup_type === 'pvp' ? 'tint-primary text-primary' : 'tint-neutral'"
          >
            {{ lineup.lineup_type.toUpperCase() }}
          </span>
          <span v-if="lineup.author">{{ lineup.author }}</span>
          <span v-if="lineup.submitted_at">{{ lineup.submitted_at }}</span>
        </div>
      </div>
      <span
        v-if="lineup.blood_magic"
        class="shrink-0 rounded border border-current/20 px-1.5 py-0.5 text-[10px] ink-muted"
      >
        {{ lineup.blood_magic }}
      </span>
    </div>

    <p v-if="lineup.intro" class="mt-1.5 line-clamp-2 text-xs ink-muted">
      {{ lineup.intro }}
    </p>

    <!-- 6 只成员：等宽列 + 换行，**不做重叠堆叠**。
         旧版用 -ml 做负边距重叠，但每列只有 48px 宽，而双属性精灵的
         两个 chip 需要约 66px——相邻成员的 chip 会贴在一起，
         看起来像"火翼""恶虫"这样的合并标签，反而读不出属性。

         现在每列固定 80px（立绘放大到 md=56px 后仍需留出 chip 的宽度），
         且允许换行：容器够宽时 6 只排一行，变窄时自动折成两行。 -->
    <div class="mt-3 flex flex-wrap gap-x-2.5 gap-y-3">
      <div
        v-for="(member, i) in members"
        :key="`${member.name}-${i}`"
        class="flex w-20 flex-col items-center transition-transform duration-200 hover:-translate-y-1"
        :title="member.name"
      >
        <div
          class="bg-base-100 rounded-full p-0.5"
          :class="highlight && member.name === highlight ? 'ring-2' : ''"
          :style="
            highlight && member.name === highlight
              ? { ...attributeStyle(member.attributes?.[0] || ''), '--tw-ring-color': 'var(--attr)' }
              : undefined
          "
        >
          <!-- md（56px）而不是 sm（40px）：阵容卡上的立绘是用户辨认精灵的
               主要依据，40px 时细节（耳朵/翅膀/配色）基本看不清。 -->
          <PetAvatar
            :name="member.name"
            :src="member.image_url"
            :attribute="member.attributes?.[0] || ''"
            size="md"
          />
        </div>
        <span class="mt-1 w-full truncate text-center text-[11px] ink-muted">
          {{ member.name }}
        </span>
        <span v-if="member.attributes?.length" class="mt-0.5 flex flex-wrap justify-center gap-0.5">
          <AttributeChip
            v-for="attr in member.attributes.slice(0, 2)"
            :key="attr"
            :type="attr"
            size="xs"
          />
        </span>
      </div>
    </div>

    <!-- 属性弱点（仅详情接口带） -->
    <div v-if="weakTo.length" class="mt-2 flex flex-wrap items-center gap-1 text-[10px]">
      <span class="ink-subtle">弱点</span>
      <span
        v-for="item in weakTo"
        :key="item.type"
        class="tint-error ink-error rounded px-1 py-px"
      >
        {{ item.type }}×{{ item.count }}
      </span>
    </div>
  </article>
</template>
