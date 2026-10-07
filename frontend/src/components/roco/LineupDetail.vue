<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { rocoApi } from '@/api/roco'
import type { Lineup, LineupMember } from '@/api/roco-types'
import { IV_FIELDS, STAT_FIELDS, statBarClass } from './visual'
import PetAvatar from './PetAvatar.vue'
import SkillIcon from './SkillIcon.vue'
import AttributeChip from './AttributeChip.vue'

const props = defineProps<{ lineup: Lineup; highlight?: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const detail = ref<Lineup | null>(null)
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  try {
    detail.value = await rocoApi.lineupDetail(props.lineup.id)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    loading.value = false
  }
})

const members = computed<LineupMember[]>(() => detail.value?.members || [])

/** 六维条形图的上限。与 PetGrid 保持一致，否则同一只精灵
 *  在两个页面上的条长不一样，看起来像数据不一致。 */
const STAT_MAX = 160
function ratio(value: number): number {
  return Math.max(0.02, Math.min(1, Number(value || 0) / STAT_MAX))
}

/** 个体值：数值化配置优先（来自 MIT 数据源），没有就退化到方向标签。 */
function ivValue(member: LineupMember, key: string): number {
  return Number(member.iv_config?.[key] || 0)
}

function hasIvConfig(member: LineupMember): boolean {
  return Object.keys(member.iv_config || {}).length > 0
}

/** 阵容码：只有 B 站专栏那个源有，没有就不显示这一块。 */
const importCode = computed(() => detail.value?.import_code || '')
const copied = ref(false)
let copyTimer: ReturnType<typeof setTimeout> | undefined

async function copyImportCode(): Promise<void> {
  const code = importCode.value
  if (!code) return
  try {
    await navigator.clipboard.writeText(code)
  } catch {
    // 剪贴板 API 在非 HTTPS / 无权限时会拒绝。**不能静默失败**——
    // 用户点了"复制"却什么都没发生，会以为按钮坏了。
    // 退化成让用户手动选中。
    error.value = '浏览器拒绝了剪贴板访问，请手动选中下方阵容码复制。'
    return
  }
  copied.value = true
  if (copyTimer) clearTimeout(copyTimer)
  copyTimer = setTimeout(() => {
    copied.value = false
  }, 2000)
}
</script>

<template>
  <div class="modal modal-open" role="dialog">
    <div class="modal-box glass-panel rise-in flex max-h-[90vh] max-w-5xl flex-col p-0">
      <header class="border-base-content/10 flex items-start justify-between gap-3 border-b p-4">
        <div class="min-w-0">
          <h3 class="truncate text-base font-semibold">{{ detail?.title || lineup.title }}</h3>
          <div class="mt-1 flex flex-wrap items-center gap-1.5 text-xs ink-muted">
            <span
              class="rounded px-1 text-[10px] font-medium"
              :class="lineup.lineup_type === 'pvp' ? 'tint-primary text-primary' : 'tint-neutral'"
            >
              {{ lineup.lineup_type.toUpperCase() }}
            </span>
            <span v-if="lineup.author">作者：{{ lineup.author }}</span>
            <span v-if="lineup.submitted_at">{{ lineup.submitted_at }}</span>
            <span v-if="lineup.blood_magic" class="rounded border border-current/20 px-1.5 py-0.5">
              血脉魔法：{{ lineup.blood_magic }}
            </span>
          </div>
        </div>
        <button type="button" class="btn btn-ghost btn-sm btn-square" @click="emit('close')">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
            <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
          </svg>
        </button>
      </header>

      <div class="scrollbar-slim min-h-0 flex-1 overflow-y-auto p-4">
        <div v-if="loading" class="space-y-3 py-4">
          <!-- 骨架屏：形状贴近真实内容（简介 + 两张分析卡 + 成员卡） -->
          <div class="skeleton h-12 w-full" />
          <div class="grid gap-2 sm:grid-cols-2">
            <div class="skeleton h-16 w-full" />
            <div class="skeleton h-16 w-full" />
          </div>
          <div class="skeleton h-40 w-full" />
        </div>
        <div v-else-if="error" class="alert alert-error">{{ error }}</div>

        <template v-else-if="detail">
          <p v-if="detail.intro" class="bg-base-content/5 mb-4 rounded-lg p-3 text-sm">
            {{ detail.intro }}
          </p>

          <!-- 游戏内阵容码：玩家复制后可在编队界面直接粘贴复现。
               只有 B 站专栏那个数据源带这个字段。 -->
          <div v-if="importCode" class="surface mb-4 p-3">
            <div class="mb-2 flex items-center justify-between gap-2">
              <p class="text-xs font-medium">游戏内阵容码</p>
              <button
                type="button"
                class="btn btn-xs"
                :class="copied ? 'btn-success' : 'btn-ghost'"
                @click="copyImportCode"
              >
                {{ copied ? '已复制' : '复制' }}
              </button>
            </div>
            <textarea
              readonly
              rows="3"
              class="textarea textarea-bordered w-full font-mono text-[11px] leading-relaxed"
              :value="importCode"
              @focus="($event.target as HTMLTextAreaElement).select()"
            />
            <p class="mt-1.5 text-[11px] ink-subtle">
              复制后进入游戏「编队」界面粘贴即可载入这套配置。
            </p>
          </div>

          <!-- 属性分析：由克制矩阵算出，不是模型推测 -->
          <div v-if="detail.type_analysis" class="mb-4 grid gap-2 sm:grid-cols-2">
            <div class="surface p-3">
              <p class="mb-1.5 text-xs font-medium">被克制（风险）</p>
              <div class="flex flex-wrap gap-1">
                <span
                  v-for="item in detail.type_analysis.weak_to.slice(0, 8)"
                  :key="item.type"
                  class="tint-error ink-error rounded px-1.5 py-px text-[11px]"
                >
                  {{ item.type }}×{{ item.count }}
                </span>
                <span v-if="!detail.type_analysis.weak_to.length" class="text-xs ink-subtle">无</span>
              </div>
            </div>
            <div class="surface p-3">
              <p class="mb-1.5 text-xs font-medium">攻击打不动</p>
              <div class="flex flex-wrap gap-1">
                <span
                  v-for="type in detail.type_analysis.gaps"
                  :key="type"
                  class="tint-warning ink-warning rounded px-1.5 py-px text-[11px]"
                >
                  {{ type }}
                </span>
                <span v-if="!detail.type_analysis.gaps.length" class="text-xs ink-subtle">全覆盖</span>
              </div>
            </div>
          </div>

          <!-- 成员：每只一张卡，含头像、性格、血脉、个体值、技能图标 -->
          <h4 class="mb-2 text-sm font-semibold">阵容成员（{{ members.length }}）</h4>
          <div class="space-y-3">
            <div
              v-for="(member, i) in members"
              :key="member.slot"
              class="surface rise-in p-3"
              :class="{ 'border-warning/50 bg-warning/5': member.pet_base_name === highlight }"
              :style="{ '--i': String(i) }"
            >
              <div class="flex items-start gap-3">
                <PetAvatar
                  :name="member.pet_name"
                  :src="member.image_url"
                  :attribute="member.attributes?.[0] || ''"
                  size="lg"
                />

                <div class="min-w-0 flex-1">
                  <div class="flex flex-wrap items-center gap-1.5">
                    <span class="text-[10px] tabular-nums ink-subtle">
                      {{ String(member.slot).padStart(2, '0') }}
                    </span>
                    <span class="font-medium">{{ member.pet_name }}</span>
                    <span
                      v-if="member.pet_base_name === highlight"
                      class="rounded bg-warning/20 px-1.5 py-px text-[10px] font-medium"
                    >
                      你选中的
                    </span>
                    <AttributeChip
                      v-for="attr in member.attributes || []"
                      :key="attr"
                      :type="attr"
                      size="xs"
                    />
                  </div>

                  <div class="mt-1.5 flex flex-wrap gap-1.5 text-[11px]">
                    <span v-if="member.nature" class="rounded border border-current/15 px-1.5 py-px">
                      性格 {{ member.nature }}
                    </span>
                    <span v-if="member.bloodline" class="rounded border border-current/15 px-1.5 py-px">
                      血脉 {{ member.bloodline }}
                    </span>
                  </div>

                  <!-- 六维种族值 -->
                  <div
                    v-if="member.stats && member.stats.total"
                    class="mt-2 grid grid-cols-3 gap-x-3 gap-y-0.5"
                  >
                    <div
                      v-for="stat in STAT_FIELDS"
                      :key="stat.key"
                      class="flex items-center gap-1.5"
                    >
                      <span class="w-7 shrink-0 text-[9px] ink-subtle">{{ stat.label }}</span>
                      <div class="bg-base-content/10 h-1 flex-1 overflow-hidden rounded-full">
                        <div
                          class="h-full rounded-full"
                          :class="statBarClass(Number(member.stats[stat.key] || 0))"
                          :style="{ width: `${ratio(member.stats[stat.key]) * 100}%` }"
                        />
                      </div>
                      <span class="w-6 shrink-0 text-right text-[9px] tabular-nums ink-muted">
                        {{ member.stats[stat.key] ?? '-' }}
                      </span>
                    </div>
                  </div>
                </div>
              </div>

              <!-- 个体值：有数值就画条形图，否则显示方向标签 -->
              <div class="mt-3">
                <p class="mb-1 text-[10px] font-medium ink-muted">个体值</p>
                <div v-if="hasIvConfig(member)" class="grid grid-cols-3 gap-x-3 gap-y-1">
                  <div v-for="iv in IV_FIELDS" :key="iv.key" class="flex items-center gap-1.5">
                    <span class="w-7 shrink-0 text-[9px] ink-subtle">{{ iv.label }}</span>
                    <div class="bg-base-content/10 h-1.5 flex-1 overflow-hidden rounded-full">
                      <!-- 个体值上限 60（实测数据里最大值），画满表示满个体 -->
                      <div
                        class="h-full rounded-full transition-all"
                        :class="ivValue(member, iv.key) >= 60 ? 'bg-success' : 'bg-warning/60'"
                        :style="{ width: `${Math.min(100, (ivValue(member, iv.key) / 60) * 100)}%` }"
                      />
                    </div>
                    <span class="w-6 shrink-0 text-right text-[9px] tabular-nums ink-muted">
                      {{ ivValue(member, iv.key) }}
                    </span>
                  </div>
                </div>
                <div v-else-if="member.talents?.length" class="flex flex-wrap gap-1">
                  <span
                    v-for="t in member.talents"
                    :key="t"
                    class="rounded tint-warning px-1.5 py-px text-[11px]"
                  >
                    {{ t }}
                  </span>
                  <span class="text-[10px] ink-subtle">（仅方向，无具体数值）</span>
                </div>
                <p v-else class="text-[10px] ink-subtle">未标注</p>
              </div>

              <!-- 技能：带图标 -->
              <div class="mt-3">
                <p class="mb-1 text-[10px] font-medium ink-muted">携带技能</p>
                <div class="grid gap-1.5 sm:grid-cols-2">
                  <div
                    v-for="skill in member.skill_details || []"
                    :key="skill.name"
                    class="bg-base-content/5 flex items-center gap-2 rounded-lg px-2 py-1.5"
                    :title="skill.description"
                  >
                    <SkillIcon :src="skill.icon_url" :name="skill.name" :attribute="skill.attribute" />
                    <div class="min-w-0 flex-1">
                      <div class="flex items-center gap-1.5">
                        <span class="truncate text-xs font-medium">{{ skill.name }}</span>
                        <AttributeChip :type="skill.attribute" size="xs" />
                      </div>
                      <div class="text-[10px] ink-muted">
                        {{ skill.category }}
                        <template v-if="skill.power"> · 威力 {{ skill.power }}</template>
                        <template v-if="skill.cost"> · 耗能 {{ skill.cost }}</template>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <p class="mt-4 text-[11px] ink-subtle">
            数据来源：{{ detail.source }}。阵容为玩家投稿，不代表对局使用率或强度排名。
            <a
              v-if="detail.source_url"
              :href="detail.source_url"
              target="_blank"
              rel="noopener noreferrer"
              class="link"
            >
              查看 BWIKI 原页面
            </a>
          </p>
        </template>
      </div>

      <footer class="border-base-content/10 flex justify-end border-t p-3">
        <button type="button" class="btn btn-sm" @click="emit('close')">关闭</button>
      </footer>
    </div>
    <div class="modal-backdrop bg-black/30" @click="emit('close')" />
  </div>
</template>
