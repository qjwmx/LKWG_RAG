import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authApi } from '@/api'
import { clearToken, getToken, setToken } from '@/api/http'
import type { CurrentUser } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<CurrentUser | null>(null)
  const ready = ref(false)

  const isLoggedIn = computed(() => !!user.value)
  const isAdmin = computed(() => user.value?.is_admin === true)
  const displayName = computed(() => user.value?.display_name || user.value?.username || '')

  async function login(username: string, password: string): Promise<void> {
    const result = await authApi.login(username, password)
    setToken(result.access_token)
    // 登录响应里的字段与 /auth/me 略有差异（没有 is_admin），
    // 统一以 /auth/me 为准，避免两处各拼一份用户对象而漂移。
    await fetchMe()
  }

  async function register(username: string, password: string, displayName: string): Promise<string> {
    const result = await authApi.register(username, password, displayName)
    return result.status
  }

  /** 用本地 token 换当前用户。token 无效时会抛 401，由调用方决定是否跳登录。 */
  async function fetchMe(): Promise<void> {
    user.value = await authApi.me()
  }

  /** 应用启动时恢复登录态。没有 token 就直接结束，不发无谓的请求。 */
  async function bootstrap(): Promise<void> {
    try {
      if (getToken()) {
        await fetchMe()
      }
    } catch {
      // token 过期/被改坏都走这里，静默清掉即可——不该在启动时报错打扰用户
      clearToken()
      user.value = null
    } finally {
      ready.value = true
    }
  }

  /**
   * 只清本地登录态，**不发任何请求**。
   *
   * 专供 401 拦截器使用：那里拿到的令牌**已经失效**，再调 `/auth/logout`
   * 只会又拿到一个 401，而 401 又会触发同一个拦截器 —— 死循环。
   * 所以 401 路径必须走这个不发请求的版本。
   */
  function clearSession(): void {
    clearToken()
    user.value = null
  }

  /**
   * 用户主动登出。
   *
   * **先通知服务端吊销令牌，再清本地**。只清本地的话，那个 JWT 在
   * 剩余有效期内仍然可用（默认 24 小时）——共享设备上"登出"等于没登出。
   *
   * 容错：**无论服务端成功与否都清本地**。网络断了也必须让用户能登出，
   * 否则会出现"点了登出还在登录态"的僵局；吊销失败最多是那个 token
   * 到自然过期为止仍有效，比卡住用户好。
   */
  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      // 网络失败 / 令牌已过期（401）：本地照常清干净，不打扰用户
    } finally {
      clearToken()
      user.value = null
    }
  }

  return {
    user,
    ready,
    isLoggedIn,
    isAdmin,
    displayName,
    login,
    register,
    fetchMe,
    bootstrap,
    logout,
    clearSession,
  }
})
