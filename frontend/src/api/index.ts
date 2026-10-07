import { get, post } from './http'
import type {
  CategoriesResult,
  ChatResult,
  CurrentUser,
  HealthResult,
  LoginResult,
  SessionSummary,
  Stats,
} from './types'

export const authApi = {
  login: (username: string, password: string) =>
    post<LoginResult>('/auth/login', { username, password }),
  register: (username: string, password: string, display_name: string) =>
    post<{ status: string }>('/auth/register', { username, password, display_name }),
  me: () => get<CurrentUser>('/auth/me'),
  /** 服务端登出：把当前 token 加入吊销名单，使其立即失效。 */
  logout: () => post<{ revoked: boolean }>('/auth/logout'),
}

export const systemApi = {
  health: () => get<HealthResult>('/system/health'),
  stats: () => get<Stats>('/system/stats'),
  categories: () => get<CategoriesResult>('/system/categories'),
}

export const chatApi = {
  /** 非流式提问。前端主链路走 SSE，这个用于降级与测试。 */
  ask: (question: string, session_id: string, category: string) =>
    post<ChatResult>('/chat/chat', { question, session_id, category, stream: false }),
  history: (session_id: string) => get<unknown[]>('/chat/history', { session_id }),
  historySessions: (limit = 30, offset = 0) =>
    get<SessionSummary[]>('/chat/sessions', { limit, offset }),
  deleteSession: (session_id: string) => post<unknown>('/chat/delete_session', { session_id }),
  feedback: (qa_log_id: string, rating: string, comment: string, session_id: string) =>
    post<unknown>('/chat/feedback', { qa_log_id, rating, comment, session_id }),
}

// 汇总出口：调用方统一从 '@/api' 取，不必记住哪个对象在哪个文件里。
export { knowledgeApi } from './knowledge'
export { adminApi } from './admin'
export * from './types'
