/** axios 实例与统一拦截。
 *
 *  三条约定与后端严格对应：
 *  - 业务状态放 body.code，HTTP 恒为 200；code != 200 时抛可读错误。
 *  - 401（token 缺失/失效）走真实 HTTP 状态码 -> 清 token 并跳登录。
 *  - 403（已登录但权限不够）走真实 HTTP 状态码 -> 提示"无权操作"，不跳登录。
 *
 *  把 401 与 403 分开处理是关键：混在一起的话，普通用户点了一个越权按钮
 *  就会被莫名其妙踢回登录页。
 */

import axios, { AxiosError, type AxiosRequestConfig } from 'axios'

export class ApiError extends Error {
  code: number
  constructor(code: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

const TOKEN_KEY = 'rag_token'

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

export function setToken(token: string): void {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

const http = axios.create({
  baseURL: '/api/v1',
  timeout: 120_000,
})

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

/** 未授权时的回调。由 auth store 注册，避免 api 层直接依赖 router/store
 *  （那会形成循环引用）。 */
let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler
}

http.interceptors.response.use(
  (response) => response,
  (error: AxiosError) => {
    const status = error.response?.status

    if (status === 401) {
      clearToken()
      onUnauthorized?.()
      const detail = (error.response?.data as { detail?: string } | undefined)?.detail
      return Promise.reject(new ApiError(401, detail || '登录已过期，请重新登录。'))
    }

    if (status === 403) {
      const detail = (error.response?.data as { detail?: string } | undefined)?.detail
      // 刻意不清 token、不跳登录：身份是有效的，只是权限不够
      return Promise.reject(new ApiError(403, detail || '当前账号没有执行该操作的权限。'))
    }

    if (error.code === 'ECONNABORTED') {
      return Promise.reject(new ApiError(0, '请求超时，请稍后重试。'))
    }

    if (!error.response) {
      return Promise.reject(
        new ApiError(0, '无法连接后端服务，请确认后端已启动（默认 127.0.0.1:8000）。'),
      )
    }

    return Promise.reject(new ApiError(status || 0, error.message))
  },
)

/** 拆掉 BaseResponse 外壳，返回 data。code != 200 时抛 ApiError。 */
async function unwrap<T>(promise: Promise<{ data: { code: number; msg: string; data: T } }>): Promise<T> {
  const response = await promise
  const payload = response.data
  if (payload.code !== 200) {
    throw new ApiError(payload.code, payload.msg || `接口返回 ${payload.code}`)
  }
  return payload.data
}

export function get<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return unwrap<T>(http.get(url, { params }))
}

export function post<T>(url: string, body?: unknown, config?: AxiosRequestConfig): Promise<T> {
  return unwrap<T>(http.post(url, body, config))
}

/** 上传文件。单独暴露是因为它需要 onUploadProgress，且 body 是 FormData。 */
export function upload<T>(
  url: string,
  form: FormData,
  onProgress?: (percent: number) => void,
): Promise<T> {
  return unwrap<T>(
    http.post(url, form, {
      onUploadProgress: (event) => {
        if (onProgress && event.total) {
          onProgress(Math.round((event.loaded / event.total) * 100))
        }
      },
    }),
  )
}

export default http
