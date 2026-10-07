import { computed, ref, shallowRef, triggerRef } from 'vue'
import { defineStore } from 'pinia'
import { rocoApi, streamStrategy } from '@/api/roco'
import type {
  AgentName,
  AttributeCount,
  Lineup,
  Pet,
  StrategyEvent,
  Teammate,
  WebSource,
  WebStatus,
} from '@/api/roco-types'
import {
  AGENT_META,
  AGENT_ORDER,
  applyAgentStatus,
  emptyAgents,
  type AgentRuntime,
} from '@/agents'

/** 生成攻略会话 id。
 *
 *  与 chat store 的 ``newId`` 同款：只用 Math.random + 时间戳，
 *  不引入 uuid 依赖。**服务端会再拼上用户名前缀**
 *  （见 checkpoint.thread_id_for），所以这里的值不是安全边界——
 *  它只负责"同一用户的不同会话彼此区分"。 */
function newId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36)
}

// 元信息与运行时状态定义在 @/agents，与对话模式共用一份：
// 两个 store 互相 import 会形成循环依赖，而 Vue/Pinia 的循环依赖
// 表现为"某个 store 初始化时是 undefined"，报错位置离原因很远。
export { AGENT_META, AGENT_ORDER }
export type { AgentRuntime, AgentState } from '@/agents'

export const useRocoStore = defineStore('roco', () => {
  /* ------------------------------------------------------------------ 图鉴 */
  const pets = ref<Pet[]>([])
  const petsTotal = ref(0)
  const petKeyword = ref('')
  const selectedPet = ref<Pet | null>(null)
  const petsLoading = ref(false)
  /** 已勾选的属性（OR 语义：任选一个就算命中） */
  const petAttributes = ref<string[]>([])
  const petMinTotal = ref(0)
  const petSort = ref('no')
  const attributeCounts = ref<AttributeCount[]>([])

  /* ------------------------------------------------------------------ 阵容 */
  const lineups = shallowRef<Lineup[]>([])
  const teammates = ref<Teammate[]>([])
  const lineupsTotal = ref(0)
  const lineupsLoading = ref(false)
  const lineupType = ref<'pvp' | 'pve'>('pvp')

  /* ------------------------------------------------------------------ 攻略 */
  const agents = ref<Record<AgentName, AgentRuntime>>(emptyAgents())
  const report = ref('')
  const reportLineups = shallowRef<Lineup[]>([])
  const findings = ref<
    Array<{ unit_id: string; topic: string; summary: string; evidence: string[] }>
  >([])
  const gaps = ref<string[]>([])
  const revisions = ref(0)
  const maxRevisions = ref(3)
  const capped = ref(false)
  const researching = ref(false)
  const researchError = ref('')

  /* 多轮追问：会话 id 决定 checkpoint 线程归属。
   * **必须复用同一个 id**，否则每轮都是全新线程，追问就没有上下文了。 */
  const strategySessionId = ref('')
  /** 这个会话已有历史（上一轮结论）。前端据此显示「继续追问」提示。 */
  const hasStrategyHistory = ref(false)
  /** 上一轮的问题，用于在界面上体现"这是追问" */
  const lastStrategyQuery = ref('')

  /* 联网检索 */
  const useWeb = ref(false)
  const webStatus = ref<WebStatus | null>(null)
  /** 本轮抓到的网页出处。空数组 = 没联网或网上没找到。 */
  const webSources = shallowRef<WebSource[]>([])
  /** 联网失败/不可用的原因。**与 researchError 不同**——报告照常产出。 */
  const webNotice = ref('')

  /* 非响应式的流控变量：见 chat store 里的同款说明 */
  let controller: AbortController | null = null
  let rafId: number | null = null
  let buffer = ''

  const isPvp = computed(() => lineupType.value === 'pvp')
  const hasReport = computed(() => report.value.length > 0)

  /* ------------------------------------------------------------------ 图鉴动作 */

  async function loadPets(keyword = petKeyword.value): Promise<void> {
    petsLoading.value = true
    try {
      const result = await rocoApi.pets({
        keyword,
        attributes: petAttributes.value,
        min_total: petMinTotal.value,
        sort: petSort.value,
        limit: 120,
      })
      pets.value = result.pets
      petsTotal.value = result.total
      petKeyword.value = keyword
      // 属性计数只在首次（或后端返回时）更新——它是全库统计，
      // 不随筛选变化，每次重算没意义。
      if (result.attribute_counts?.length) {
        attributeCounts.value = result.attribute_counts
      }
    } finally {
      petsLoading.value = false
    }
  }

  /** 勾选/取消一个属性，立即重新筛选。 */
  async function toggleAttribute(type: string): Promise<void> {
    const next = petAttributes.value.includes(type)
      ? petAttributes.value.filter((t) => t !== type)
      : [...petAttributes.value, type]
    petAttributes.value = next
    await loadPets()
  }

  async function clearPetFilters(): Promise<void> {
    petAttributes.value = []
    petMinTotal.value = 0
    petSort.value = 'no'
    petKeyword.value = ''
    await loadPets('')
  }

  async function setPetSort(sort: string): Promise<void> {
    petSort.value = sort
    await loadPets()
  }

  async function setPetMinTotal(value: number): Promise<void> {
    petMinTotal.value = value
    await loadPets()
  }

  async function selectPet(name: string): Promise<void> {
    selectedPet.value = await rocoApi.pet(name)
  }

  /* ------------------------------------------------------------------ 阵容动作 */

  /** ★ 按精灵反查阵容。这是「选精灵 → 查库里的阵容」的入口。 */
  async function loadLineupsByPet(petName: string): Promise<void> {
    lineupsLoading.value = true
    try {
      const result = await rocoApi.lineupsByPet(petName, lineupType.value)
      lineups.value = result.lineups
      teammates.value = result.teammates
      lineupsTotal.value = result.total
    } finally {
      lineupsLoading.value = false
    }
  }

  async function loadLineups(keyword = ''): Promise<void> {
    lineupsLoading.value = true
    try {
      const result = await rocoApi.lineups(lineupType.value, keyword, 40)
      lineups.value = result.lineups
      lineupsTotal.value = result.total
      teammates.value = []
    } finally {
      lineupsLoading.value = false
    }
  }

  async function setLineupType(value: 'pvp' | 'pve'): Promise<void> {
    lineupType.value = value
    if (selectedPet.value) {
      await loadLineupsByPet(selectedPet.value.name)
    } else {
      await loadLineups()
    }
  }

  /* ------------------------------------------------------------------ 攻略流 */

  function resetAgents(): void {
    agents.value = emptyAgents()
  }

  /** 用 rAF 合并正文更新：一次报告有几十上百个 delta，
   *  逐个赋值会触发同样多次组件 patch。 */
  function flushBuffer(): void {
    rafId = null
    report.value = buffer
  }

  function appendDelta(text: string): void {
    buffer += text
    if (rafId === null) rafId = requestAnimationFrame(flushBuffer)
  }

  function applyEvent(event: StrategyEvent): void {
    if (event.type === 'delta') {
      appendDelta(event.content)
      return
    }

    if (event.type === 'agent_status') {
      applyAgentStatus(agents.value, event)
      triggerRef(agents)
      return
    }

    if (event.type === 'web_unavailable') {
      // 用户开了联网但服务端没配。**提前说**，不要等到 done——
      // 否则用户会以为"开关点了但什么都没发生"。
      webNotice.value = event.message
      return
    }

    if (event.type === 'done') {
      // 用 done 里的完整报告覆盖：流式正文可能被截断或有兜底补发，
      // done 里的才是落定的版本
      buffer = event.report
      report.value = event.report
      reportLineups.value = event.lineups || []
      findings.value = event.findings || []
      gaps.value = event.gaps || []
      revisions.value = event.revisions
      maxRevisions.value = event.max_revisions
      capped.value = event.capped
      webSources.value = event.web_sources || []
      // 联网失败不是致命错误，报告已经产出了，只是要如实告知没联上网
      if (event.web_error) webNotice.value = `联网检索未成功：${event.web_error}`
      // 服务端回显的 session_id 以它为准（可能被归一化过）
      if (event.session_id) strategySessionId.value = event.session_id
      return
    }

    if (event.type === 'error') {
      researchError.value = event.message
    }
  }

  /** 确保有会话 id。**同一会话的多轮追问必须复用同一个值**。 */
  function ensureStrategySession(): string {
    if (!strategySessionId.value) {
      strategySessionId.value = `strat_${newId()}`
    }
    return strategySessionId.value
  }

  /** 开一个新会话，丢弃追问上下文。 */
  function newStrategySession(): void {
    strategySessionId.value = `strat_${newId()}`
    hasStrategyHistory.value = false
    lastStrategyQuery.value = ''
    report.value = ''
    reportLineups.value = []
    findings.value = []
    gaps.value = []
    webSources.value = []
    webNotice.value = ''
    researchError.value = ''
    resetAgents()
  }

  /** 拉取联网可用性。失败不报错——按钮显示"不可用"即可，不该打扰用户。 */
  async function loadWebStatus(): Promise<void> {
    try {
      webStatus.value = await rocoApi.webStatus()
    } catch {
      webStatus.value = null
    }
  }

  const webAvailable = computed(() => webStatus.value?.available === true)

  async function research(payload: {
    query: string
    targetPet?: string
    maxRevisions?: number
  }): Promise<void> {
    if (researching.value) return

    const sessionId = ensureStrategySession()

    resetAgents()
    report.value = ''
    reportLineups.value = []
    findings.value = []
    gaps.value = []
    revisions.value = 0
    capped.value = false
    researchError.value = ''
    webSources.value = []
    webNotice.value = ''
    buffer = ''
    researching.value = true
    controller = new AbortController()

    try {
      await streamStrategy({
        query: payload.query,
        target_pet: payload.targetPet || '',
        lineup_type: lineupType.value,
        max_revisions: payload.maxRevisions ?? 3,
        session_id: sessionId,
        // 服务端没配联网时不要发 true：发过去只会换来一条
        // web_unavailable 事件，白跑一趟
        use_web: useWeb.value && webAvailable.value,
        signal: controller.signal,
        onEvent: applyEvent,
      })
      // 只有跑完才把这一轮记成"上一轮"。中途失败不该让用户以为
      // 已经有一轮结论可以追问了。
      if (!researchError.value) {
        lastStrategyQuery.value = payload.query
        hasStrategyHistory.value = true
      }
    } catch (exc) {
      // AbortError 是用户主动停止，不是失败
      const aborted = exc instanceof DOMException && exc.name === 'AbortError'
      if (!aborted) {
        researchError.value = exc instanceof Error ? exc.message : String(exc)
      }
    } finally {
      if (rafId !== null) {
        cancelAnimationFrame(rafId)
        rafId = null
      }
      report.value = buffer
      researching.value = false
      controller = null
    }
  }

  function stopResearch(): void {
    controller?.abort()
    controller = null
    researching.value = false
  }

  return {
    pets,
    petsTotal,
    petKeyword,
    selectedPet,
    petsLoading,
    petAttributes,
    petMinTotal,
    petSort,
    attributeCounts,
    lineups,
    teammates,
    lineupsTotal,
    lineupsLoading,
    lineupType,
    isPvp,
    agents,
    report,
    reportLineups,
    findings,
    gaps,
    revisions,
    maxRevisions,
    capped,
    researching,
    researchError,
    hasReport,
    strategySessionId,
    hasStrategyHistory,
    lastStrategyQuery,
    useWeb,
    webStatus,
    webAvailable,
    webSources,
    webNotice,
    newStrategySession,
    loadWebStatus,
    loadPets,
    toggleAttribute,
    clearPetFilters,
    setPetSort,
    setPetMinTotal,
    selectPet,
    loadLineupsByPet,
    loadLineups,
    setLineupType,
    research,
    stopResearch,
    resetAgents,
  }
})
