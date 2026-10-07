/** 与后端契约对应的类型。字段名一律保持 snake_case，与接口逐字一致——
 *  在这里做驼峰转换只会让"接口返回了什么"变得难以对照。 */

export interface BaseResponse<T> {
  code: number
  msg: string
  data: T | null
}

export interface LoginResult {
  username: string
  display_name: string
  role: 'admin' | 'user'
  access_token: string
  token_type: string
  expires_in: number
}

export interface CurrentUser {
  username: string
  display_name: string
  role: 'admin' | 'user'
  is_admin: boolean
  allow_admin_promotion: boolean
}

export interface UserRecord {
  username: string
  display_name: string
  role: 'admin' | 'user'
  status: 'pending' | 'approved' | 'rejected'
  created_at: string
  updated_at: string
}

export interface UserListResult {
  users: UserRecord[]
  counts: Record<string, number>
  admin_username: string
  allow_admin_promotion: boolean
}

export interface DocumentRecord {
  id: string
  file_name: string
  file_type: string
  title: string
  category: string
  version: string
  owner: string
  scope: 'public' | 'private'
  text_length: number
  chunk_count: number
  status: 'processing' | 'success' | 'failed'
  error: string
  uploaded_at: string
  updated_at: string
}

/** 回答里的一条引用。chunk_index 用于定位，snippet 用于卡片预览。 */
export interface SourceDoc {
  document_id: string
  chunk_index: number
  title: string
  category: string
  version: string
  file_name: string
  score: number
  snippet: string
}

/** 联网检索回来的一条网页来源。
 *
 *  **与 ``SourceDoc`` 分开两个类型**：本地引用能点开看切片并高亮原文，
 *  网络来源只能跳外链——两者的前端行为完全不同，混成一个类型就得靠
 *  字段猜测，而且 ``ChunkViewer`` 会拿到一个没有 document_id 的条目。 */
export interface WebSource {
  title: string
  url: string
  snippet: string
  source_type: 'web'
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  created_at: string
  /** 助手消息才有 */
  qa_log_id?: string
  question_type?: string
  source_docs?: SourceDoc[]
  /** 本轮联网检索到的网络来源（未开启联网时为空） */
  web_sources?: WebSource[]
  /** 联网失败的可读原因。**与 error 不同**——回答是好的，只是没联上网。 */
  web_error?: string
  /** 流式进行中 */
  streaming?: boolean
  /** 该轮失败时的可读错误 */
  error?: string
  /** 用户已提交的反馈 */
  feedback?: 'helpful' | 'needs_improvement' | null
}

export interface SessionSummary {
  session_id: string
  question: string
  category: string
  username: string
  turns: number
  started_at: string
  last_at: string
}

export interface ChatResult {
  answer: string
  question_type: string
  qa_log_id: string
  source_docs: SourceDoc[]
  web_sources: WebSource[]
  web_error: string
}

export interface ChunkRecord {
  chunk_index: number
  content: string
}

export interface ChunksResult {
  document: DocumentRecord
  chunks: ChunkRecord[]
}

export interface UploadedItem {
  file_name: string
  status: 'success' | 'duplicate' | 'failed'
  message: string
  document_id: string | null
}

export interface UploadResult {
  uploaded: UploadedItem[]
  failed_files: Record<string, string>
}

export interface Stats {
  document_count: number
  category_count: number
  qa_count: number
  feedback_count: number
  user_count?: number
  pending_user_count?: number
}

export interface HealthResult {
  status: 'ok' | 'degraded'
  postgres: { ok: boolean; detail: string }
  embedding: { ok: boolean; provider: string; detail: string }
  llm: { ok: boolean; detail: string }
  /** Redis 是可选增强：没配时 ok=true（走进程内缓存），
   *  配了却连不上才 ok=false。 */
  redis: { ok: boolean; configured: boolean; detail: string }
}

export interface CategoriesResult {
  categories: string[]
  filter_options: string[]
  supported_file_types: string[]
  default_version: string
  upload_max_mb: number
}

export interface FeedbackRecord {
  id: string
  qa_log_id: string
  rating: string
  comment: string
  session_id: string
  created_at: string
  updated_at: string
}

export interface QaLogRecord {
  id: string
  session_id: string
  username: string
  question: string
  answer: string
  category: string
  question_type: string
  source_docs: SourceDoc[]
  created_at: string
}

/* ------------------------------------------------------------------ 可观测性
 *
 * 全部要求管理员；普通用户会拿到 403（由 http.ts 转成可读错误）。
 * 这些类型只承载**结构性信息**（路由、状态、耗时、token），
 * 后端刻意不采集请求体 / query string / 口令 / token。
 */

export interface LatencyStats {
  avg_ms: number
  p50_ms: number
  p95_ms: number
  p99_ms: number
  max_ms: number
  sample_size: number
}

export interface WriterStats {
  queue_size: number
  queue_capacity: number
  /** 已成功落库的轨迹数 */
  written: number
  /** 队列满时被丢弃的轨迹数。**必须显示**——否则统计数字会静静地少一块 */
  dropped: number
  running: boolean
}

export interface SlowRoute {
  route: string
  calls: number
  avg_ms: number
  max_ms: number
}

export interface ObservabilityOverview {
  window_hours: number
  requests: number
  errors: number
  error_rate: number
  qpm: number
  total_tokens: number
  llm_calls: number
  avg_tokens_per_request: number
  latency: LatencyStats
  /** 上游没返回用量的调用数。与"确实用了 0 token"是两件事 */
  usage_missing_calls: number
  writer: WriterStats
  slowest_routes: SlowRoute[]
  notice: string
}

export interface RequestTraceRow {
  id: string
  request_id: string
  route: string
  method: string
  status: number
  duration_ms: number
  username: string
  error: string
  llm_calls: number
  total_tokens: number
  created_at: string
}

export interface RequestTraceList {
  requests: RequestTraceRow[]
  total: number
}

export interface AgentUsage {
  agent: string
  calls: number
  total_tokens: number
  input_tokens: number
  output_tokens: number
  avg_duration_ms: number
  max_duration_ms: number
  /** 占窗口内总 token 的比例（0~1） */
  token_share: number
}

export interface LlmBreakdown {
  window_hours: number
  agents: AgentUsage[]
  total_tokens: number
}

export interface CacheStatus {
  ok: boolean
  configured: boolean
  detail: string
  stats: {
    redis: boolean
    corpus_version?: string
    local_entries?: Record<string, number>
    keys?: number
  }
}
