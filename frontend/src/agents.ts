/** 五 Agent 流水线的共享元信息与运行时状态。
 *
 *  对话模式（chat store）与攻略模式（roco store）跑的是同一套
 *  Planner → Researcher → Analyst → Writer → Reviewer 骨架，
 *  进度事件的形状也一致，所以元信息与状态类型定义在这里共用一份。
 *
 *  放在 store 之外的原因：两个 store 互相 import 会形成循环依赖，
 *  而 Vue/Pinia 的循环依赖表现为"某个 store 初始化时是 undefined"，
 *  报错位置离真正的原因很远。
 */

import type { AgentName } from '@/api/roco-types'

/** 五个 Agent 的展示元信息。顺序即流水线顺序。 */
export const AGENT_ORDER: AgentName[] = [
  'Planner',
  'Researcher',
  'Analyst',
  'Writer',
  'Reviewer',
]

export const AGENT_META: Record<AgentName, { label: string; desc: string }> = {
  Planner: { label: '规划', desc: '拆解研究问题' },
  Researcher: { label: '检索', desc: '从知识库取证' },
  Analyst: { label: '分析', desc: '交叉验证与去重' },
  Writer: { label: '撰写', desc: '输出回答' },
  Reviewer: { label: '审阅', desc: '检查依据与表述' },
}

export type AgentState = 'idle' | 'running' | 'done' | 'failed'

export interface AgentRuntime {
  state: AgentState
  stage: string
  detail: string
  percent: number
  /** 并行 Researcher 的每个单元 */
  units: Array<{ unit_id: string; state: AgentState; detail: string }>
}

export function emptyAgents(): Record<AgentName, AgentRuntime> {
  return AGENT_ORDER.reduce(
    (acc, name) => {
      acc[name] = { state: 'idle', stage: '', detail: '', percent: 0, units: [] }
      return acc
    },
    {} as Record<AgentName, AgentRuntime>,
  )
}

/** 把一条 agent_status 事件合并进运行时状态。
 *
 *  两个 store 共用同一套合并逻辑：分头实现的话，"哪个 stage 算完成"
 *  这种细节会在两边漂移，表现为同一个后端事件在一个页面显示"完成"、
 *  在另一个页面显示"进行中"。
 */
export function applyAgentStatus(
  agents: Record<AgentName, AgentRuntime>,
  event: {
    agent: AgentName
    stage: string
    detail?: string
    percent?: number
    unit_id?: string
  },
): void {
  const runtime = agents[event.agent]
  if (!runtime) return

  runtime.stage = event.stage
  runtime.detail = event.detail || ''
  if (typeof event.percent === 'number') runtime.percent = event.percent

  if (event.stage === 'failed') {
    runtime.state = 'failed'
  } else if (event.stage === 'done' || event.stage === 'final') {
    runtime.state = 'done'
  } else {
    runtime.state = 'running'
  }

  // Researcher 是并行的：按 unit_id 分列，否则多个单元的事件会互相覆盖，
  // 前端只能看到"最后那个"的进度。
  if (event.agent === 'Researcher' && event.unit_id) {
    let unit = runtime.units.find((u) => u.unit_id === event.unit_id)
    if (!unit) {
      unit = { unit_id: event.unit_id, state: 'running', detail: '' }
      runtime.units.push(unit)
    }
    unit.detail = event.detail || ''
    unit.state = event.stage === 'done' ? 'done' : event.stage === 'empty' ? 'failed' : 'running'
  }
}

/** 流水线跑完时，把所有**启动过**的 Agent 标为完成。
 *
 *  为什么需要它：图里只有部分节点会发 "done"/"final" 阶段的进度事件
 *  （Writer 发完 drafting 就去生成正文、Reviewer 发完 reviewing 就结束了），
 *  所以最后一帧进度面板会显示"四个 Agent 还在跑"。
 *
 *  收到 ``done`` 事件意味着整张图已经跑完，此时没失败的 Agent 必然是完成的；
 *  没启动过的（预检短路时一个都没跑）保持 idle，不能一律改成 done，
 *  否则界面会假装跑过五个 Agent。
 */
export function finishAgents(agents: Record<AgentName, AgentRuntime>): void {
  for (const name of AGENT_ORDER) {
    const runtime = agents[name]
    if (runtime.state === 'idle' || runtime.state === 'failed') continue
    runtime.state = 'done'
    runtime.percent = 100
    for (const unit of runtime.units) {
      if (unit.state === 'running') unit.state = 'done'
    }
  }
}

/** 流水线是否已经跑出过内容——用来决定要不要显示进度面板。
 *
 *  只跑过一次的用户提问才显示；历史会话里的旧消息不该带进度条。
 */
export function hasProgress(agents: Record<AgentName, AgentRuntime>): boolean {
  return AGENT_ORDER.some((name) => agents[name].state !== 'idle')
}
