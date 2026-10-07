/** SSE 流式客户端。
 *
 *  为什么手写而不用 @microsoft/fetch-event-source：
 *  1. 后端只发 `data:` 帧，不用 SSE 的 event:/id:/retry: 字段，
 *     完整 SSE 解析器在这里属于过度设计；
 *  2. 手写能拿到完整的 AbortController 控制——"停止生成"要求立刻中断
 *     HTTP 流，而不是等下一个事件到达；
 *  3. fetch-event-source 在 npm 上的最新版本停在 2021 年（2.0.1），
 *     不适合作为长期依赖。
 *
 *  原生 EventSource 用不了：它只支持 GET、不能带请求体、不能自定义 header，
 *  而问答是 POST + JSON body + Authorization header。
 *
 *  必须自己处理"跨 chunk 半包"：网络分片不保证按 `\n\n` 边界到达，
 *  一次 read() 可能拿到半个 JSON，也可能一次拿到三个完整帧。
 */

import { getToken } from './http'
import type { AgentName } from './roco-types'
import type { WebSource } from './types'

/** 对话流的事件。
 *
 *  后端跑的是五 Agent 检索流水线，因此除了正文帧，还会先发一串进度帧
 *  （``agent_status`` / ``artifact``）。它们与攻略模式共用同一套形状。
 */
export interface SseEvent {
  type: 'delta' | 'done' | 'error' | 'agent_status' | 'artifact' | 'reset' | 'web_unavailable'
  content?: string
  message?: string
  qa_log_id?: string
  answer?: string
  question_type?: string
  source_docs?: unknown[]
  /** 本轮联网抓到的网页（已按 url 去重） */
  web_sources?: WebSource[]
  /** 联网失败的可读原因。**与 error 不同**——回答是好的，只是没联上网。 */
  web_error?: string
  references_markdown?: string
  /** 进度帧才有 */
  agent?: AgentName
  unit_id?: string
  stage?: string
  detail?: string
  percent?: number
  kind?: string
  payload?: unknown
  /** done 帧里的五 Agent 运行痕迹 */
  units?: Array<{ unit_id: string; topic: string; reason: string }>
  findings?: Array<{
    unit_id: string
    topic: string
    summary: string
    evidence: string[]
  }>
  revisions?: number
}

export interface StreamOptions {
  question: string
  session_id: string
  category?: string
  /** 本轮是否允许联网检索。**由用户界面上的高亮开关决定**。 */
  use_web?: boolean
  signal?: AbortSignal
  onEvent: (event: SseEvent) => void
}

export async function streamChat(options: StreamOptions): Promise<void> {
  const { question, session_id, category = '全部', use_web = false, signal, onEvent } = options

  const response = await fetch('/api/v1/chat/chat', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
    body: JSON.stringify({ question, session_id, category, stream: true, use_web }),
    signal,
  })

  // 401/403 是真实 HTTP 状态码（不是 body.code），要单独识别
  if (response.status === 401) {
    throw new Error('登录已过期，请重新登录。')
  }
  if (response.status === 403) {
    throw new Error('当前账号没有执行该操作的权限。')
  }
  if (!response.ok) {
    throw new Error(`请求失败（HTTP ${response.status}）`)
  }
  if (!response.body) {
    throw new Error('服务端未返回流式响应体。')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })

      // SSE 以空行分隔帧。用 \n\n 切分，最后一段可能是不完整的帧，留在 buffer 里。
      let separator = buffer.indexOf('\n\n')
      while (separator !== -1) {
        const rawFrame = buffer.slice(0, separator)
        buffer = buffer.slice(separator + 2)
        handleFrame(rawFrame, onEvent)
        separator = buffer.indexOf('\n\n')
      }
    }

    // 流结束时 buffer 里可能还剩最后一帧（服务端没补末尾空行）
    if (buffer.trim()) {
      handleFrame(buffer, onEvent)
    }
  } finally {
    reader.releaseLock()
  }
}

function handleFrame(rawFrame: string, onEvent: (event: SseEvent) => void): void {
  // 一帧可能有多行，但后端只发一行 data:；按规范把所有 data: 行拼起来
  const dataLines: string[] = []
  for (const line of rawFrame.split('\n')) {
    const trimmed = line.trimEnd()
    if (trimmed.startsWith('data:')) {
      dataLines.push(trimmed.slice(5).trimStart())
    }
  }
  if (!dataLines.length) return

  const payload = dataLines.join('\n')
  if (!payload) return

  try {
    const event = JSON.parse(payload) as SseEvent
    onEvent(event)
  } catch {
    // 单个坏帧不该中断整条流——丢掉它继续读
    console.warn('[sse] 无法解析的帧：', payload.slice(0, 200))
  }
}
