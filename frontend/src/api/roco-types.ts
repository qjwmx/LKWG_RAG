/** 洛克王国领域类型。字段名与后端逐字一致（snake_case），不做驼峰转换——
 *  转换只会让"接口返回了什么"难以对照。 */

export interface Pet {
  id: string
  no: number
  name: string
  form: string
  attributes: string[]
  stats: Record<string, number>
  ability: { name?: string; description?: string }
  evolution_chain: unknown
  has_shiny: boolean
  wiki_url: string
  /** 精灵头像 URL（BWIKI 热链）。空串表示没有图。 */
  image_url: string
  skill_names: string[]
  source: string
  /** 详情接口才带 */
  skills?: Record<string, Skill>
  forms?: string[]
}

export interface Skill {
  name: string
  attribute: string
  category: string
  power: number
  cost: number
  description: string
  /** 技能图标 URL。空串表示没有图。 */
  icon_url?: string
}

export interface LineupMember {
  slot: number
  pet_name: string
  pet_base_name: string
  bloodline: string
  nature: string
  talents: string[]
  iv_config: Record<string, number>
  skills: string[]
  skill_details?: Skill[]
  /** 成员精灵头像与属性（详情接口带） */
  image_url?: string
  attributes?: string[]
  stats?: Record<string, number>
}

/** 阵容卡片上的成员缩略信息 */
export interface MemberImage {
  name: string
  image_url: string
  attributes: string[]
}

export interface AttributeCount {
  type: string
  count: number
}

export interface Lineup {
  id: string
  wiki_id: string
  title: string
  lineup_type: string
  author: string
  intro: string
  blood_magic: string
  magic_label: string
  source_url: string
  source: string
  submitted_at: string
  /**
   * 游戏内「阵容码」——复制后可在编队界面粘贴复现这套阵容。
   * **可能为空串**：只有 B 站专栏那个数据源带它，BWIKI 与种子数据没有。
   */
  import_code: string
  member_names: string[]
  member_images?: MemberImage[]
  members?: LineupMember[]
  type_analysis?: TypeAnalysis | null
}

export interface TypeCount {
  type: string
  count: number
}

export interface TypeAnalysis {
  weak_to: TypeCount[]
  resist: TypeCount[]
  coverage: string[]
  gaps: string[]
}

export interface Teammate {
  pet_name: string
  together_count: number
  total_lineups: number
}

export interface DomainStats {
  game: string
  pets: number
  skills: number
  lineups: number
  lineup_members: number
  type_matchup: number
  notice: string
  lineup_filter_options: string[]
}

/** 攻略模式：五 Agent 流水线事件 */
export type AgentName = 'Planner' | 'Researcher' | 'Analyst' | 'Writer' | 'Reviewer'

/** 联网检索回来的一条网页结果。
 *
 *  ``source_type`` 恒为 ``"web"``：前端靠它把「网络来源」与本地库资料
 *  分组渲染。**必须让用户一眼看出这是外部内容**——未经核实，
 *  和玩家投稿阵容的可信度不是一回事。 */
export interface WebSource {
  title: string
  url: string
  snippet: string
  source_type: 'web'
}

/** 联网可用性（``GET /strategy/web_status``）。**不含任何 key 内容**。 */
export interface WebStatus {
  /** 服务端总开关 */
  enabled: boolean
  /** 是否配了 TAVILY_API_KEY */
  configured: boolean
  /** enabled && configured，前端据此决定按钮能不能点 */
  available: boolean
  provider: string
  max_results: number
}

export interface AgentStatusEvent {
  type: 'agent_status'
  agent: AgentName
  unit_id?: string
  stage: string
  detail?: string
  percent?: number
}

export interface ArtifactEvent {
  type: 'artifact'
  agent: AgentName
  kind: string
  payload: unknown
}

export interface DeltaEvent {
  type: 'delta'
  content: string
}

/** 用户开了联网但服务端没配。**提前告知**，不然用户会以为开关坏了。 */
export interface WebUnavailableEvent {
  type: 'web_unavailable'
  message: string
}

export interface StrategyDoneEvent {
  type: 'done'
  report: string
  lineups: Lineup[]
  revisions: number
  max_revisions: number
  capped: boolean
  units: Array<{ unit_id: string; topic: string; reason: string }>
  findings: Array<{
    unit_id: string
    topic: string
    summary: string
    evidence: string[]
    lineup_ids: string[]
  }>
  gaps: string[]
  /** 本轮联网抓到的网页（已按 url 去重）。空数组表示没联网或没抓到。 */
  web_sources: WebSource[]
  /** 联网失败的原因。**与 error 不同**——报告照常产出，只是没联上网。 */
  web_error: string
  /** 本轮所属的攻略会话 id，用于多轮追问 */
  session_id: string
}

export interface StrategyErrorEvent {
  type: 'error'
  message: string
}

export type StrategyEvent =
  | AgentStatusEvent
  | ArtifactEvent
  | DeltaEvent
  | WebUnavailableEvent
  | StrategyDoneEvent
  | StrategyErrorEvent
