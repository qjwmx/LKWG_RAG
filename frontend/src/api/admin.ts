import { get, post } from './http'
import type {
  CacheStatus,
  FeedbackRecord,
  LlmBreakdown,
  ObservabilityOverview,
  QaLogRecord,
  RequestTraceList,
  UserListResult,
} from './types'

/** 管理后台接口。全部要求管理员；非管理员会得到 403（由 http.ts 转成可读错误）。 */
export const adminApi = {
  /** 用户列表。审批页默认用 status='pending'，并轮询它显示侧边栏角标。 */
  users: (status?: 'pending' | 'approved' | 'rejected') =>
    get<UserListResult>('/admin/users', status ? { status } : undefined),

  review: (username: string, status: 'pending' | 'approved' | 'rejected') =>
    post<unknown>('/admin/users/review', { username, status }),

  setRole: (username: string, role: 'admin' | 'user') =>
    post<unknown>('/admin/users/role', { username, role }),

  deleteUser: (username: string) => post<unknown>('/admin/users/delete', { username }),

  qaLogs: (limit?: number) =>
    get<QaLogRecord[]>('/admin/qa_logs', limit ? { limit } : undefined),

  feedback: () =>
    get<{
      feedback: FeedbackRecord[]
      summary: { total: number; helpful: number; needs_improvement: number }
    }>('/admin/feedback'),
}

/** 可观测性。**仅管理员**——后端在 admin router 上挂了 require_admin，
 *  普通用户直接调这些接口会拿到 403。前端隐藏入口不是安全边界。 */
export const observabilityApi = {
  overview: (hours = 24) =>
    get<ObservabilityOverview>('/admin/observability/overview', { hours }),

  requests: (params: {
    limit?: number
    offset?: number
    route?: string
    status?: string
    username?: string
  } = {}) => get<RequestTraceList>('/admin/observability/requests', params),

  llm: (hours = 24) => get<LlmBreakdown>('/admin/observability/llm', { hours }),

  cache: () => get<CacheStatus>('/admin/observability/cache'),
}
