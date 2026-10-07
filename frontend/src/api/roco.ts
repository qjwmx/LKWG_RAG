import { get, post } from './http'
import { getToken } from './http'
import type {
  AttributeCount,
  DomainStats,
  Lineup,
  Pet,
  Skill,
  StrategyEvent,
  Teammate,
  WebStatus,
} from './roco-types'

/** 洛克王国领域接口。 */
export const rocoApi = {
  // ---------------------------------------------------------------- 图鉴
  /** 精灵图鉴，支持属性筛选 / 种族值下限 / 排序。 */
  pets: (params: {
    keyword?: string
    attributes?: string[]
    min_total?: number
    sort?: string
    limit?: number
    offset?: number
  } = {}) =>
    get<{
      pets: Pet[]
      total: number
      attribute_counts: AttributeCount[]
    }>('/pokedex/pets', {
      keyword: params.keyword || '',
      attributes: (params.attributes || []).join(','),
      min_total: params.min_total || 0,
      sort: params.sort || 'no',
      limit: params.limit ?? 60,
      offset: params.offset ?? 0,
    }),

  pet: (name: string) => get<Pet>('/pokedex/pet', { name }),

  skills: (keyword = '', limit = 100) =>
    get<Skill[]>('/pokedex/skills', { keyword, limit }),

  domainStats: () => get<DomainStats>('/pokedex/stats'),

  // ---------------------------------------------------------------- 阵容
  /** ★ 按精灵反查阵容 —— 核心能力 */
  lineupsByPet: (pet_name: string, lineup_type = 'pvp') =>
    get<{ pet_name: string; total: number; lineups: Lineup[]; teammates: Teammate[] }>(
      '/lineups/by_pet',
      { pet_name, lineup_type },
    ),

  lineups: (lineup_type = 'pvp', keyword = '', limit = 30, offset = 0) =>
    get<{ lineups: Lineup[]; total: number }>('/lineups/list', {
      lineup_type,
      keyword,
      limit,
      offset,
    }),

  lineupDetail: (lineup_id: string) => get<Lineup>('/lineups/detail', { lineup_id }),

  topPets: (limit = 20, lineup_type = 'pvp') =>
    get<{ pets: Array<{ pet_name: string; lineup_count: number }>; notice: string }>(
      '/lineups/top_pets',
      { limit, lineup_type },
    ),

  // ---------------------------------------------------------------- 攻略研究
  researchSync: (payload: {
    query: string
    target_pet?: string
    lineup_type?: string
    max_revisions?: number
    session_id?: string
    use_web?: boolean
  }) => post<{ report: string; lineups: Lineup[] }>('/strategy/research_sync', payload),

  /** 联网可用性。前端据此决定「允许联网」按钮能不能点。 */
  webStatus: () => get<WebStatus>('/strategy/web_status'),
}

/**
 * 攻略模式 SSE 流。
 *
 * 与对话模式的 ``streamChat`` 是同一套帧解析逻辑，但事件类型不同
 * （agent_status / artifact / delta / done），所以单独一个函数而不是塞进去。
 * 同样必须用 fetch 而不是原生 EventSource：需要 POST body 与 Authorization 头。
 */
export async function streamStrategy(options: {
  query: string
  target_pet?: string
  lineup_type?: string
  max_revisions?: number
  /** 攻略会话 id，决定多轮追问的上下文归属 */
  session_id?: string
  /** 本轮是否允许联网检索 */
  use_web?: boolean
  signal?: AbortSignal
  onEvent: (event: StrategyEvent) => void
}): Promise<void> {
  const {
    query,
    target_pet = '',
    lineup_type = 'pvp',
    max_revisions = 3,
    session_id = '',
    use_web = false,
    signal,
    onEvent,
  } = options

  const response = await fetch('/api/v1/strategy/research', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
    body: JSON.stringify({ query, target_pet, lineup_type, max_revisions, session_id, use_web }),
    signal,
  })

  if (response.status === 401) throw new Error('登录已过期，请重新登录。')
  if (response.status === 403) throw new Error('当前账号没有执行该操作的权限。')
  if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}）`)
  if (!response.body) throw new Error('服务端未返回流式响应体。')

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })

      let separator = buffer.indexOf('\n\n')
      while (separator !== -1) {
        const rawFrame = buffer.slice(0, separator)
        buffer = buffer.slice(separator + 2)
        handleFrame(rawFrame, onEvent)
        separator = buffer.indexOf('\n\n')
      }
    }
    if (buffer.trim()) handleFrame(buffer, onEvent)
  } finally {
    reader.releaseLock()
  }
}

function handleFrame(rawFrame: string, onEvent: (event: StrategyEvent) => void): void {
  const dataLines: string[] = []
  for (const line of rawFrame.split('\n')) {
    const trimmed = line.trimEnd()
    if (trimmed.startsWith('data:')) dataLines.push(trimmed.slice(5).trimStart())
  }
  if (!dataLines.length) return
  const payload = dataLines.join('\n')
  if (!payload) return
  try {
    onEvent(JSON.parse(payload) as StrategyEvent)
  } catch {
    // 单个坏帧不该中断整条流
    console.warn('[strategy-sse] 无法解析的帧：', payload.slice(0, 200))
  }
}
