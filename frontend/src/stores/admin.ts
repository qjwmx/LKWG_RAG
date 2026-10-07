import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { adminApi } from '@/api/admin'
import type { UserRecord } from '@/api/types'

/** 管理后台状态。
 *
 *  pendingCount 会被侧边栏角标用，所以单独轮询——它必须在任何后台页面上
 *  都保持最新，而不是只有打开审批页时才更新。
 */
export const useAdminStore = defineStore('admin', () => {
  const users = ref<UserRecord[]>([])
  const counts = ref<Record<string, number>>({})
  const adminUsername = ref('')
  const allowPromotion = ref(true)
  const loading = ref(false)
  const error = ref('')

  let pollTimer: number | null = null

  const pendingCount = computed(() => counts.value.pending ?? 0)
  const pendingUsers = computed(() => users.value.filter((u) => u.status === 'pending'))
  const approvedUsers = computed(() => users.value.filter((u) => u.status === 'approved'))
  const rejectedUsers = computed(() => users.value.filter((u) => u.status === 'rejected'))

  async function loadUsers(status?: 'pending' | 'approved' | 'rejected'): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      const result = await adminApi.users(status)
      users.value = result.users
      counts.value = result.counts
      adminUsername.value = result.admin_username
      allowPromotion.value = result.allow_admin_promotion
    } catch (exc) {
      error.value = exc instanceof Error ? exc.message : String(exc)
      throw exc
    } finally {
      loading.value = false
    }
  }

  /** 只刷新角标数量，不覆盖当前列表——否则在"已通过"标签页上轮询
   *  会把列表悄悄换成"待审批"，用户正在看的行会凭空消失。 */
  async function refreshCounts(): Promise<void> {
    try {
      const result = await adminApi.users()
      counts.value = result.counts
    } catch {
      // 轮询失败静默：网络抖动不该在界面上闪错误
    }
  }

  async function review(
    username: string,
    status: 'pending' | 'approved' | 'rejected',
  ): Promise<string> {
    await adminApi.review(username, status)
    await refreshCounts()
    const label =
      status === 'approved' ? '已通过审批' : status === 'rejected' ? '已拒绝' : '已重置为待审批'
    return `${username} ${label}。`
  }

  async function setRole(username: string, role: 'admin' | 'user'): Promise<void> {
    await adminApi.setRole(username, role)
  }

  async function removeUser(username: string): Promise<void> {
    await adminApi.deleteUser(username)
    users.value = users.value.filter((u) => u.username !== username)
    await refreshCounts()
  }

  /** 开始轮询待审批数。页面不可见时暂停——后台标签页不该持续打接口。 */
  function startPolling(intervalMs = 30_000): void {
    stopPolling()
    void refreshCounts()
    pollTimer = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        void refreshCounts()
      }
    }, intervalMs)
  }

  function stopPolling(): void {
    if (pollTimer !== null) {
      window.clearInterval(pollTimer)
      pollTimer = null
    }
  }

  return {
    users,
    counts,
    adminUsername,
    allowPromotion,
    loading,
    error,
    pendingCount,
    pendingUsers,
    approvedUsers,
    rejectedUsers,
    loadUsers,
    refreshCounts,
    review,
    setRole,
    removeUser,
    startPolling,
    stopPolling,
  }
})
