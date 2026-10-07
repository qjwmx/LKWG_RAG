import { computed, ref, shallowRef } from 'vue'
import { defineStore } from 'pinia'
import { chatApi } from '@/api'
import { rocoApi } from '@/api/roco'
import { streamChat } from '@/api/sse'
import type { ChatMessage, SessionSummary, SourceDoc, WebSource } from '@/api/types'
import type { WebStatus } from '@/api/roco-types'
import { applyAgentStatus, emptyAgents, finishAgents, type AgentRuntime } from '@/agents'
import type { AgentName } from '@/api/roco-types'

function newId(): string {
  return Math.random().toString(36).slice(2) + Date.now().toString(36)
}

export const useChatStore = defineStore('chat', () => {
  /* ------------------------------------------------------------------ 状态
   *
   * messages 用 shallowRef：消息对象很大（含答案全文与引用数组），
   * 用 ref 会让每个元素都被深度代理，流式追加时 diff 成本明显。
   * 代价是**每次更新都要整体换掉数组引用**（见 patchMessage），
   * 就地改或只换元素都不会让视图更新。
   */
  const messages = shallowRef<ChatMessage[]>([])
  const sessions = ref<SessionSummary[]>([])
  const sessionId = ref('')
  const category = ref('全部')
  const isStreaming = ref(false)
  const sessionsLoading = ref(false)

  /* 联网检索开关。
   *
   * **默认关**：Tavily 按次计费，且联网内容是外部不可信数据。
   * 界面上的按钮高亮 = 会联网，不亮 = 不联网（用户一眼能看出当前状态）。
   *
   * 做成**持久**的（跟着会话走）而不是每轮重置：用户开了联网通常
   * 是想连着问几个问题，每轮都要重新点一次会很烦。 */
  const useWeb = ref(false)
  const webStatus = ref<WebStatus | null>(null)

  /** 服务端是否真的能用联网（配了 key 且没被禁用）。
   *  没配时按钮禁用并显示「未配置」——比让用户点了没反应要好。 */
  const webAvailable = computed(() => webStatus.value?.available === true)

  /** 五 Agent 检索流水线的实时进度。
   *  用 shallowRef：AgentRuntime 是就地修改的，深度代理在这里没有收益。 */
  const agents = shallowRef<Record<AgentName, AgentRuntime>>(emptyAgents())

  /** 就地改完 agents 之后调用它，**换掉顶层对象引用**。
   *
   *  为什么不能只 triggerRef：`agents` 是作为 prop 传给 ``PipelineProgress``
   *  的，而 Vue 比较 props 用 ``Object.is``。顶层对象引用不变时，
   *  Vue 判定"props 未变化"→ 跳过子组件更新，进度条会**冻在第一帧**。
   *  换引用让 props 真的变了，同时只浅拷贝 5 个键，开销可忽略。
   */
  function bumpAgents(): void {
    agents.value = { ...agents.value }
  }

  /* 以下三个刻意是**非响应式**的普通变量：
   * - streamBuffer：每个 token 都改它，做成响应式等于每个 token 触发一次渲染
   * - rafId：纯粹的调度句柄，与视图无关
   * - controller：AbortController 被 Vue 代理后 abort() 行为不可靠
   */
  let streamBuffer = ''
  let rafId: number | null = null
  let controller: AbortController | null = null
  let streamingMessageId = ''

  const hasMessages = computed(() => messages.value.length > 0)
  const lastMessage = computed(() => messages.value[messages.value.length - 1])
  /** 流水线是否已经启动过——用来决定要不要显示进度面板 */
  const pipelineActive = computed(() => isStreaming.value)

  function ensureSession(): string {
    if (!sessionId.value) {
      sessionId.value = `rag_${newId()}`
    }
    return sessionId.value
  }

  /** 更新某条消息的内容，**必须整体换掉数组引用**。
   *
   *  这是本项目最隐蔽的一个 bug，踩过一次：
   *
   *  ``messages`` 是 ``shallowRef``，同时作为 prop 传给 ``MessageList``。
   *  Vue 比较 props 用的是 ``Object.is``（引用相等）。如果就地改元素
   *  （``list[i].content = x``）或只换元素但保留数组
   *  （``list[i] = {...}; triggerRef(messages)``），**数组引用都没变**，
   *  Vue 判定 ``messages`` 这个 prop 未变化 → **跳过整个子树更新**，
   *  ``v-for`` 不会重跑，``ChatMessage`` 也拿不到新对象。
   *
   *  症状：store 里明明有完整答案（`isStreaming=false`、`content` 长度正常），
   *  界面上却永远停在「正在检索知识库…」，而且**不报任何错**。
   *
   *  代价说明：每个 token 复制一次数组。消息数是个位数到几十，
   *  浅拷贝可忽略；真正贵的是深度代理消息对象，而那正是用 shallowRef 避开的。
   */
  function patchMessage(id: string, patch: Partial<ChatMessage>): void {
    const list = messages.value
    const index = list.findIndex((m) => m.id === id)
    if (index === -1) return
    const next = list.slice()
    next[index] = { ...next[index], ...patch }
    messages.value = next
  }

  function newSession(): void {
    stopStreaming()
    sessionId.value = `rag_${newId()}`
    messages.value = []
  }

  function setSession(id: string): void {
    if (id === sessionId.value) return
    // 切会话必须中断在途的流，否则旧流的 delta 会写进新会话——
    // 这是最容易被忽略的一类竞态。
    stopStreaming()
    sessionId.value = id
    messages.value = []
  }

  /* ------------------------------------------------------------------ 流式 */

  function flushBuffer(): void {
    rafId = null
    patchMessage(streamingMessageId, { content: streamBuffer })
  }

  function appendDelta(text: string): void {
    streamBuffer += text
    // 用 rAF 合并高频更新：一次回答可能有上千个 delta，
    // 逐个赋值会让 Vue 触发上千次组件 patch。
    if (rafId === null) {
      rafId = requestAnimationFrame(flushBuffer)
    }
  }

  function stopStreaming(): void {
    if (controller) {
      controller.abort()
      controller = null
    }
    if (rafId !== null) {
      cancelAnimationFrame(rafId)
      rafId = null
    }
    // 收尾时把缓冲区里最后一点内容落定，否则会丢掉最后几个 token
    patchMessage(streamingMessageId, { content: streamBuffer, streaming: false })
    isStreaming.value = false
    streamingMessageId = ''
    streamBuffer = ''
  }

  async function send(question: string): Promise<void> {
    const text = question.trim()
    if (!text || isStreaming.value) return

    const currentSession = ensureSession()
    const userMessage: ChatMessage = {
      id: newId(),
      role: 'user',
      content: text,
      created_at: new Date().toISOString(),
    }
    const assistantMessage: ChatMessage = {
      id: newId(),
      role: 'assistant',
      content: '',
      created_at: new Date().toISOString(),
      streaming: true,
      source_docs: [],
      web_sources: [],
    }

    messages.value = [...messages.value, userMessage, assistantMessage]
    streamingMessageId = assistantMessage.id
    streamBuffer = ''
    isStreaming.value = true
    // 每轮重置流水线：上一轮的"完成"状态留着会让本轮看起来已经跑完了
    agents.value = emptyAgents()

    controller = new AbortController()

    try {
      await streamChat({
        question: text,
        session_id: currentSession,
        category: category.value,
        // 服务端没配联网时不要发 true：发过去只会换来一条
        // web_unavailable 事件，白跑一趟
        use_web: useWeb.value && webAvailable.value,
        signal: controller.signal,
        onEvent: (event) => {
          // 五 Agent 的进度帧。前端按 type 分发，这里只关心进度，
          // 正文仍然走 delta —— 进度事件不参与内容拼接。
          if (event.type === 'agent_status') {
            applyAgentStatus(agents.value, {
              agent: event.agent!,
              stage: event.stage || '',
              detail: event.detail,
              percent: event.percent,
              unit_id: event.unit_id,
            })
            bumpAgents()
            return
          }
          if (event.type === 'artifact') {
            // 拆解结果等结构化产物：目前只在进度面板里体现为"已完成"，
            // 不单独渲染卡片，避免和引用卡片抢注意力。
            return
          }
          if (event.type === 'reset') {
            // Writer 重写：后端会重新把整篇正文流一遍，而 delta 是增量语义，
            // 前端只会 append。不清空的话用户会看到"第一版 + 第二版"接在一起。
            streamBuffer = ''
            patchMessage(assistantMessage.id, { content: '' })
            return
          }
          if (event.type === 'delta' && event.content) {
            appendDelta(event.content)
            return
          }
          if (event.type === 'done') {
            // 正文流已结束。用 done 里的完整 answer 覆盖，
            // 保证"界面所见 == 库里所存"（流式正文不含引用段落）。
            //
            // ★ web_error **只在 done 真的带了原因时才覆盖**。
            // 早先写的是 `web_error: event.web_error || ''`，于是
            // 前面 `web_unavailable` 设好的提示会被这里的空串**抹掉**——
            // 用户看到提示闪一下就没了，表现为"开关点了没反应"，
            // 而那正是这条提示要解决的问题。不联网成功时 done 的
            // web_error 本来就是空串，所以必须保留已有值。
            const patch: Partial<ChatMessage> = {
              ...(event.answer ? { content: event.answer } : {}),
              qa_log_id: event.qa_log_id,
              question_type: event.question_type,
              source_docs: (event.source_docs || []) as SourceDoc[],
              // 网络来源单独一组：可信度与本地引用不同，UI 上要分开
              web_sources: (event.web_sources || []) as WebSource[],
              streaming: false,
            }
            if (event.web_error) patch.web_error = event.web_error
            patchMessage(assistantMessage.id, patch)
            // 整张图跑完了：把启动过的 Agent 都标成完成，
            // 否则进度面板会停在"Writer 还在写"（它发完 drafting 就结束了）
            finishAgents(agents.value)
            bumpAgents()
            return
          }
          if (event.type === 'web_unavailable') {
            // 用户开了联网但服务端没配。**提前说**，不要等到 done——
            // 否则用户会以为"开关点了但什么都没发生"。
            // 落在这条消息上而不是全局，因为它只影响这一轮。
            patchMessage(assistantMessage.id, { web_error: event.message || '联网不可用' })
            return
          }
          if (event.type === 'error') {
            patchMessage(assistantMessage.id, {
              error: event.message || '回答失败',
              streaming: false,
            })
          }
        },
      })
    } catch (exc) {
      // AbortError 是用户主动"停止生成"，不是错误，不该显示为失败
      const aborted = exc instanceof DOMException && exc.name === 'AbortError'
      if (!aborted) {
        patchMessage(assistantMessage.id, {
          error: exc instanceof Error ? exc.message : String(exc),
          streaming: false,
        })
      }
    } finally {
      stopStreaming()
    }
  }

  /* ------------------------------------------------------------------ 历史 */

  async function loadSessions(): Promise<void> {
    sessionsLoading.value = true
    try {
      sessions.value = await chatApi.historySessions()
    } finally {
      sessionsLoading.value = false
    }
  }

  /** 拉取联网可用性。失败不报错——按钮显示不可用即可，不该打扰用户。 */
  async function loadWebStatus(): Promise<void> {
    try {
      webStatus.value = await rocoApi.webStatus()
    } catch {
      webStatus.value = null
    }
  }

  async function openSession(id: string): Promise<void> {
    setSession(id)
    const raw = (await chatApi.history(id)) as Array<Record<string, unknown>>
    messages.value = raw.map((item) => ({
      id: newId(),
      role: item.role as 'user' | 'assistant',
      content: String(item.content || ''),
      created_at: String(item.created_at || ''),
      qa_log_id: item.qa_log_id ? String(item.qa_log_id) : undefined,
      question_type: item.question_type ? String(item.question_type) : undefined,
      source_docs: (item.source_docs || []) as SourceDoc[],
      // 网络来源同样要还原：否则重新打开会话时只剩本地引用，
      // 用户会以为当初的联网根本没生效过
      web_sources: (item.web_sources || []) as WebSource[],
      feedback: null,
    }))
    // 恢复该会话最后一次用过的知识范围
    const lastAssistant = [...raw].reverse().find((m) => m.role === 'assistant')
    if (lastAssistant?.category) {
      category.value = String(lastAssistant.category)
    }
  }

  async function deleteSession(id: string): Promise<void> {
    await chatApi.deleteSession(id)
    sessions.value = sessions.value.filter((s) => s.session_id !== id)
    if (id === sessionId.value) {
      newSession()
    }
  }

  async function submitFeedback(message: ChatMessage, rating: 'helpful' | 'needs_improvement') {
    if (!message.qa_log_id) return
    await chatApi.feedback(message.qa_log_id, rating, '', sessionId.value)
    patchMessage(message.id, { feedback: rating })
  }

  return {
    messages,
    sessions,
    sessionId,
    category,
    isStreaming,
    sessionsLoading,
    agents,
    useWeb,
    webStatus,
    webAvailable,
    pipelineActive,
    hasMessages,
    lastMessage,
    send,
    stopStreaming,
    newSession,
    setSession,
    loadSessions,
    loadWebStatus,
    openSession,
    deleteSession,
    submitFeedback,
  }
})
