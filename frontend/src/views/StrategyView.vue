<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRocoStore } from '@/stores/roco'
import type { Lineup, Pet } from '@/api/roco-types'
import AppShell from '@/components/layout/AppShell.vue'
import PetFilter from '@/components/roco/PetFilter.vue'
import PetGrid from '@/components/roco/PetGrid.vue'
import PetAvatar from '@/components/roco/PetAvatar.vue'
import AgentPipeline from '@/components/roco/AgentPipeline.vue'
import LineupCard from '@/components/roco/LineupCard.vue'
import LineupDetail from '@/components/roco/LineupDetail.vue'
import MarkdownBody from '@/components/chat/MarkdownBody.vue'
import WebToggle from '@/components/chat/WebToggle.vue'
import AttributeChip from '@/components/roco/AttributeChip.vue'

const roco = useRocoStore()
const {
  selectedPet,
  lineups,
  teammates,
  lineupsTotal,
  lineupsLoading,
  lineupType,
  agents,
  report,
  reportLineups,
  gaps,
  revisions,
  maxRevisions,
  capped,
  researching,
  researchError,
  petsTotal,
  hasStrategyHistory,
  lastStrategyQuery,
  useWeb,
  webAvailable,
  webSources,
  webNotice,
} = storeToRefs(roco)

/** 两种模式：查阵容（精确反查） / 攻略研究（五 Agent） */
const mode = ref<'lineups' | 'research'>('lineups')
const question = ref('')
const activeLineup = ref<Lineup | null>(null)
const domainNotice = ref('')
const sidebarOpen = ref(false)

onMounted(async () => {
  await roco.loadLineups()
  // 联网可用性：决定「允许联网」开关能不能点。
  // 失败不报错——按钮显示不可用即可，不该打扰用户。
  void roco.loadWebStatus()
  try {
    const stats = await import('@/api/roco').then((m) => m.rocoApi.domainStats())
    domainNotice.value = stats.notice
  } catch {
    // 拿不到概览不影响主流程
  }
})

function onPickPet(pet: Pet): void {
  roco.loadLineupsByPet(pet.name)
  // 窄屏下选完就收起侧栏，否则看不到结果
  if (window.innerWidth < 1280) sidebarOpen.value = false
}

const canResearch = computed(() => question.value.trim().length > 0 && !researching.value)

async function startResearch(): Promise<void> {
  if (!canResearch.value) return
  await roco.research({
    query: question.value.trim(),
    targetPet: selectedPet.value?.name || '',
    maxRevisions: 3,
  })
}

/** 用选中的精灵一键生成研究问题，省得用户自己组织语言 */
function fillQuestion(): void {
  const name = selectedPet.value?.name
  question.value = name
    ? `推荐一套以${name}为核心的PvP阵容，说明搭配思路和注意事项`
    : '推荐几套当前版本好用的PvP阵容'
}

const researcherUnits = computed(() => agents.value.Researcher.units)
</script>

<template>
  <AppShell>
    <div class="flex" style="height: calc(100vh - var(--shell-header-h))">
      <!-- ============ 左栏：筛选 + 精灵网格 ============ -->
      <!-- ★ glass-chrome 而不是 glass-region：这一栏里有**裸文字**
           （"精灵图鉴"标题、筛选器的各个标签），文字底下没有卡片再兜一层。
           region 只有 28%/40%，裸文字压在上面会掉到 3.8:1 / 2.0:1。
           所以它按"结构骨架"处理（与顶栏同类），够实。
           里面的 60 张精灵卡仍是 .surface，不再各自模糊。 -->
      <aside
        class="glass-chrome border-base-content/10 flex w-80 shrink-0 flex-col gap-3 border-r p-3 transition-all"
        :class="sidebarOpen ? 'absolute z-30 h-full shadow-xl' : 'hidden xl:flex'"
      >
        <div class="flex items-center justify-between">
          <h2 class="text-sm font-semibold">精灵图鉴</h2>
          <button
            type="button"
            class="btn btn-ghost btn-xs xl:hidden"
            @click="sidebarOpen = false"
          >
            收起
          </button>
        </div>
        <PetFilter />
        <div class="border-base-content/10 border-t" />
        <PetGrid class="min-h-0 flex-1" @pick="onPickPet" />
      </aside>

      <!-- ============ 主区 ============ -->
      <!-- 主区本身用**很轻**的 glass-region（28%/40%）：
           它的作用是"统一壁纸"，让卡片之间的空隙仍看得见壁纸。
           这里所有文字都在 .surface 卡片上（卡片有 55%/86% 的填充），
           所以不需要靠主区背景来保证可读性。 -->
      <div class="glass-region flex min-w-0 flex-1 flex-col">
        <!-- 顶栏：模式切换 + 当前选中。
           ★ 用 glass-chrome 而不是 bg-base-100/40：这一行有**裸文字**
             （"阵容查询 / 攻略研究"、精灵名、"465 只精灵 · 151 套阵容"），
             它们直接压在这里，底下没有卡片。原来用 40% 的浅底，
             在壁纸上会掉到 3.5:1 以下。 -->
        <div
          class="glass-chrome border-base-content/10 flex flex-wrap items-center gap-2 border-b px-3 py-2"
        >
          <button
            type="button"
            class="btn btn-ghost btn-sm btn-square xl:hidden"
            title="打开精灵图鉴"
            @click="sidebarOpen = true"
          >
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
              <path d="M3 6h18v2H3V6zm0 5h18v2H3v-2zm0 5h18v2H3v-2z" />
            </svg>
          </button>

          <!-- 分段控件：无边框、选中态浅底。比 tabs-box 更接近极简风 -->
          <div class="bg-base-content/5 inline-flex gap-0.5 rounded-lg p-0.5">
            <button
              v-for="item in [
                { key: 'lineups', label: '阵容查询' },
                { key: 'research', label: '攻略研究' },
              ]"
              :key="item.key"
              type="button"
              class="rounded-md px-3 py-1 text-xs transition-colors"
              :class="
                mode === item.key
                  ? 'tint-neutral font-medium'
                  : 'ink-subtle hover:text-base-content'
              "
              @click="mode = item.key as 'lineups' | 'research'"
            >
              {{ item.label }}
            </button>
          </div>

          <select
            v-model="lineupType"
            class="select select-xs"
            @change="roco.setLineupType(lineupType)"
          >
            <option value="pvp">PVP</option>
            <option value="pve">PVE</option>
          </select>

          <!-- 当前选中精灵：始终可见，避免用户忘了自己选的是哪只 -->
          <div
            v-if="selectedPet"
            class="bg-base-content/5 ml-auto flex items-center gap-2 rounded-full py-1 pr-3 pl-1"
          >
            <PetAvatar
              :name="selectedPet.name"
              :src="selectedPet.image_url"
              :attribute="selectedPet.attributes[0]"
              size="xs"
            />
            <span class="text-xs font-medium">{{ selectedPet.name }}</span>
            <AttributeChip
              v-for="attr in selectedPet.attributes"
              :key="attr"
              :type="attr"
              size="xs"
            />
          </div>
          <span v-else class="ml-auto text-[11px] ink-subtle">
            {{ petsTotal }} 只精灵 · {{ lineupsTotal }} 套阵容
          </span>
        </div>

        <!-- ==================== 阵容查询 ==================== -->
        <div v-if="mode === 'lineups'" class="scrollbar-slim min-h-0 flex-1 overflow-y-auto p-3">
          <!-- 数据来源声明：必须保留（防止把玩家投稿当成强度排名）。
               改用细边框提示条，而不是 alert 的大色块。

               ★ 必须带 `surface`（提供底色），不能只画边框。
               原先只有 `border` 没有背景，文字直接压在壁纸上——
               探针实测 business + 靛蓝 85% 下只有 **3.84:1**（需 4.5）。
               这是"看起来有边框像个卡片、实际没有底色"的典型陷阱：
               边框给人"这是个容器"的错觉，但对比度只看文字底下是什么。 -->
          <div
            v-if="domainNotice"
            class="surface ink-muted mb-3 px-3 py-1.5 text-[11px]"
          >
            {{ domainNotice }}
          </div>

          <!-- 队友统计：推荐阵容的数据依据 -->
          <div v-if="selectedPet && teammates.length" class="mb-4">
            <h3 class="mb-2 flex flex-wrap items-baseline gap-2 text-sm font-semibold">
              <span>{{ selectedPet.name }} 的常见队友</span>
              <span class="text-[11px] font-normal ink-muted">
                统计自 {{ teammates[0].total_lineups }} 套含它的投稿阵容
              </span>
            </h3>
            <div class="flex flex-wrap gap-1.5">
              <button
                v-for="mate in teammates"
                :key="mate.pet_name"
                type="button"
                class="border-base-content/15 hover:border-base-content/30 hover:bg-base-content/5 flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-colors"
                :title="`同队 ${mate.together_count}/${mate.total_lineups} 套`"
                @click="roco.selectPet(mate.pet_name).then(() => roco.loadLineupsByPet(mate.pet_name))"
              >
                {{ mate.pet_name }}
                <span class="tint-primary text-primary rounded px-1 tabular-nums">
                  {{ mate.together_count }}
                </span>
              </button>
            </div>
          </div>

          <!-- 骨架屏：形状与真实阵容卡一致 -->
          <div v-if="lineupsLoading" class="grid gap-3 2xl:grid-cols-2">
            <div v-for="n in 4" :key="n" class="surface p-3">
              <div class="skeleton h-4 w-32" />
              <div class="skeleton mt-2 h-3 w-48" />
              <div class="mt-3 flex gap-2">
                <div v-for="m in 6" :key="m" class="skeleton h-10 w-10 rounded-full" />
              </div>
            </div>
          </div>

          <div v-else-if="!lineups.length" class="py-16 text-center ink-muted">
            <p v-if="selectedPet">没有找到含「{{ selectedPet.name }}」的阵容。</p>
            <p v-else>从左侧选一只精灵，查看包含它的阵容。</p>
          </div>

          <template v-else>
            <h3 class="mb-2 text-sm font-semibold">
              <template v-if="selectedPet">
                含「{{ selectedPet.name }}」的阵容（{{ lineupsTotal }} 套）
              </template>
              <template v-else>全部阵容（{{ lineupsTotal }} 套）</template>
            </h3>
            <div class="grid gap-3 2xl:grid-cols-2">
              <LineupCard
                v-for="(lineup, i) in lineups"
                :key="lineup.id"
                :lineup="lineup"
                :highlight="selectedPet?.name"
                :index="i"
                @open="(l) => (activeLineup = l)"
              />
            </div>
          </template>
        </div>

        <!-- ==================== 攻略研究 ==================== -->
        <div v-else class="flex min-h-0 flex-1">
          <!-- 左：研究流水线 -->
          <div class="scrollbar-slim border-base-content/10 w-64 shrink-0 overflow-y-auto border-r p-3">
            <AgentPipeline />
            <p v-if="researcherUnits.length" class="mt-2 text-[11px] ink-muted">
              并行检索单元：{{ researcherUnits.length }} 个
            </p>
          </div>

          <!-- 右：报告 -->
          <div class="flex min-w-0 flex-1 flex-col">
            <div class="border-base-content/10 border-b p-3">
              <div class="flex gap-2">
                <input
                  v-model="question"
                  class="input input-sm flex-1"
                  placeholder="描述你的需求，例如：推荐一套以寂灭骨龙为核心的PvP阵容"
                  :disabled="researching"
                  @keydown.enter="startResearch"
                />
                <!-- ★ 用 `surface` 给底色，不能只靠 `btn-ghost`。
                     幽灵按钮的背景是**透明**的，文字实际压在壁纸上——
                     探针实测 business + 靛蓝下只有 **4.19:1**（需 4.5）。
                     `surface` 提供 55% 的底色，让对比度与壁纸无关。 -->
                <button
                  type="button"
                  class="btn btn-ghost btn-sm surface"
                  :disabled="researching"
                  title="用选中的精灵自动填一个问题"
                  @click="fillQuestion"
                >
                  填入
                </button>
                <button
                  v-if="researching"
                  type="button"
                  class="btn btn-error btn-sm"
                  @click="roco.stopResearch()"
                >
                  停止
                </button>
                <!-- 研究进行中给按钮加 aura 辉光：daisyUI 5.7 内置，
                     比"转圈图标"更能表达"正在深度工作" -->
                <div v-else class="aura aura-glow aura-sm" :class="canResearch ? 'text-primary' : 'opacity-40'">
                  <button
                    type="button"
                    class="btn btn-primary btn-sm"
                    :disabled="!canResearch"
                    @click="startResearch"
                  >
                    开始研究
                  </button>
                </div>
              </div>
              <p v-if="selectedPet" class="mt-1.5 flex items-center gap-1.5 text-[11px] ink-muted">
                核心精灵：
                <PetAvatar
                  :name="selectedPet.name"
                  :src="selectedPet.image_url"
                  :attribute="selectedPet.attributes[0]"
                  size="xs"
                />
                <b>{{ selectedPet.name }}</b>
                （会围绕它检索阵容与队友）
              </p>

              <!-- 追问提示 + 联网开关 + 新会话。
                   这三样放一行，都在"输入区"语义内。 -->
              <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                <!-- 追问上下文提示：让用户知道"这一轮会带上上一轮"，
                     否则他会困惑为什么模型记得上次说的话（或反过来抱怨没记住）。

                     ★ 用 `surface` 提供**真实底色**，不能用 `bg-base-content/5`。
                     后者只有 5% 的文字色叠加，在 business + 靛蓝壁纸下
                     实测只有 **3.71:1**（需 4.5）——因为它基本是透明的，
                     文字实际上还是压在壁纸上。`rounded-full` 覆盖 surface 的圆角。 -->
                <span
                  v-if="hasStrategyHistory"
                  class="surface inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] ink-muted"
                  :title="`本轮会带上上一轮的结论：${lastStrategyQuery}`"
                >
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    stroke-width="2"
                    stroke-linecap="round"
                    class="h-3 w-3"
                    aria-hidden="true"
                  >
                    <path d="M9 15l6-6" />
                    <path d="M11 6l1-1a4 4 0 015.66 5.66l-1 1" />
                    <path d="M13 18l-1 1a4 4 0 01-5.66-5.66l1-1" />
                  </svg>
                  继续追问中
                  <button
                    type="button"
                    class="hover:text-base-content underline decoration-dotted"
                    :disabled="researching"
                    @click="roco.newStrategySession()"
                  >
                    开新会话
                  </button>
                </span>

                <!-- 联网开关。**与对话模式共用同一个组件**（WebToggle），
                     避免两处的文案、状态与配色慢慢跑偏。
                     默认关：Tavily 按次计费，且联网内容是外部不可信数据。 -->
                <WebToggle
                  v-model="useWeb"
                  :available="webAvailable"
                  :disabled="researching"
                  compact
                />
              </div>

              <!-- 联网不可用/失败的提示。**用 info 而不是 error**：
                   报告照常产出，这只是"这次没联上网"。 -->
              <p v-if="webNotice" class="alert alert-info mt-2 py-1.5 text-[11px]">
                {{ webNotice }}
              </p>
            </div>

            <div class="scrollbar-slim min-h-0 flex-1 overflow-y-auto p-4">
              <div v-if="researchError" class="alert alert-error mb-3 text-sm">
                <span>{{ researchError }}</span>
              </div>

              <!-- 超限放行的提示：必须显式，不能让用户以为这是定稿 -->
              <div v-if="capped" class="alert alert-warning mb-3 py-2 text-xs">
                <span>
                  已达最大修订轮次（{{ maxRevisions }} 轮）后放行，报告可能仍有未解决的问题。
                </span>
              </div>
              <div v-else-if="revisions > 1" class="alert alert-info mb-3 py-2 text-xs">
                <span>经过 {{ revisions }} 轮评审后定稿。</span>
              </div>

              <div v-if="!report && !researching" class="py-20 text-center ink-muted">
                <p class="text-sm">输入需求，五个 Agent 会协作产出攻略报告。</p>
                <p class="mt-1 text-xs">
                  规划 → 检索 → 分析 → 撰写 → 审阅，全过程实时可见。
                </p>
              </div>

              <MarkdownBody v-if="report" :content="report" :streaming="researching" />

              <div v-if="gaps.length" class="alert alert-warning mt-4 py-2 text-xs">
                <div>
                  <p class="font-medium">证据缺口（本地库资料不足）</p>
                  <ul class="mt-1 list-inside list-disc">
                    <li v-for="gap in gaps" :key="gap">{{ gap }}</li>
                  </ul>
                </div>
              </div>

              <div v-if="reportLineups.length" class="mt-6">
                <h3 class="mb-2 text-sm font-semibold">报告引用的阵容</h3>
                <div class="grid gap-3 2xl:grid-cols-2">
                  <LineupCard
                    v-for="(lineup, i) in reportLineups"
                    :key="lineup.id"
                    :lineup="lineup"
                    :highlight="selectedPet?.name"
                    :index="i"
                    @open="(l) => (activeLineup = l)"
                  />
                </div>
              </div>

              <!-- 联网出处：**必须与本地库资料分组展示**。
                   混在一起的话，用户分不清哪条结论有本地数据支撑、
                   哪条只是网上抄来的说法。 -->
              <div v-if="webSources.length" class="mt-6">
                <h3 class="mb-2 flex flex-wrap items-baseline gap-2 text-sm font-semibold">
                  <span>网络来源</span>
                  <span class="text-[11px] font-normal ink-warning">
                    外部内容 · 未经验证 · 仅供补充参考
                  </span>
                </h3>
                <ul class="space-y-1.5">
                  <li v-for="source in webSources" :key="source.url" class="surface p-2.5">
                    <a
                      :href="source.url"
                      target="_blank"
                      rel="noopener noreferrer nofollow"
                      class="tint-primary text-primary text-xs font-medium hover:underline"
                    >
                      {{ source.title }}
                    </a>
                    <p class="mt-1 text-[11px] leading-relaxed ink-muted">{{ source.snippet }}</p>
                    <p class="mt-1 truncate text-[10px] ink-subtle">{{ source.url }}</p>
                  </li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <LineupDetail
      v-if="activeLineup"
      :lineup="activeLineup"
      :highlight="selectedPet?.name"
      @close="activeLineup = null"
    />
  </AppShell>
</template>
