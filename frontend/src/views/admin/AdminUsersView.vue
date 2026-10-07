<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useAdminStore } from '@/stores/admin'
import type { UserRecord } from '@/api/types'
import AdminLayout from '@/components/layout/AdminLayout.vue'

const admin = useAdminStore()
const { users, loading, error, allowPromotion, adminUsername } = storeToRefs(admin)

const tab = ref<'pending' | 'approved' | 'rejected'>('pending')
const notice = ref('')
const actionError = ref('')
const busyUser = ref('')

const tabs = [
  { key: 'pending' as const, label: '待审批' },
  { key: 'approved' as const, label: '已通过' },
  { key: 'rejected' as const, label: '已拒绝' },
]

async function load(): Promise<void> {
  await admin.loadUsers(tab.value)
}

onMounted(load)

async function switchTab(key: 'pending' | 'approved' | 'rejected'): Promise<void> {
  tab.value = key
  notice.value = ''
  actionError.value = ''
  await load()
}

async function review(username: string, status: 'pending' | 'approved' | 'rejected'): Promise<void> {
  busyUser.value = username
  notice.value = ''
  actionError.value = ''
  try {
    notice.value = await admin.review(username, status)
    await load()
  } catch (exc) {
    actionError.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busyUser.value = ''
  }
}

async function approveAll(): Promise<void> {
  const targets = admin.pendingUsers.map((u) => u.username)
  if (!targets.length) return
  if (!window.confirm(`确定一次通过全部 ${targets.length} 个待审批账号吗？`)) return

  busyUser.value = '__all__'
  actionError.value = ''
  try {
    for (const username of targets) {
      await admin.review(username, 'approved')
    }
    notice.value = `已通过 ${targets.length} 个账号。`
    await load()
  } catch (exc) {
    actionError.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busyUser.value = ''
  }
}

async function toggleRole(user: UserRecord): Promise<void> {
  const next = user.role === 'admin' ? 'user' : 'admin'
  const label = next === 'admin' ? '设为管理员' : '降为普通用户'
  if (!window.confirm(`确定把 ${user.username} ${label}吗？`)) return

  busyUser.value = user.username
  actionError.value = ''
  try {
    await admin.setRole(user.username, next)
    notice.value = `${user.username} 已${label}。`
    await load()
  } catch (exc) {
    actionError.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busyUser.value = ''
  }
}

async function removeUser(user: UserRecord): Promise<void> {
  if (
    !window.confirm(
      `确定删除用户 ${user.username} 吗？\n\n该账号将无法再登录。它上传的文档与问答记录会保留。`,
    )
  ) {
    return
  }
  busyUser.value = user.username
  actionError.value = ''
  try {
    await admin.removeUser(user.username)
    notice.value = `已删除用户 ${user.username}。`
    await load()
  } catch (exc) {
    actionError.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busyUser.value = ''
  }
}
</script>

<template>
  <AdminLayout>
    <div class="mb-4 flex flex-wrap items-center justify-between gap-2">
      <div>
        <h1 class="text-xl font-semibold">用户审批</h1>
        <p class="text-xs ink-muted">
          注册的账号处于「待审批」，<b>通过之前无法登录</b>。这里是唯一的放行入口。
        </p>
      </div>
      <button
        v-if="tab === 'pending' && admin.pendingUsers.length"
        type="button"
        class="btn btn-primary btn-sm"
        :disabled="busyUser === '__all__'"
        @click="approveAll"
      >
        <span v-if="busyUser === '__all__'" class="loading loading-spinner loading-xs" />
        全部通过（{{ admin.pendingUsers.length }}）
      </button>
    </div>

    <div v-if="notice" class="alert alert-success mb-3 py-2 text-sm">{{ notice }}</div>
    <div v-if="actionError" class="alert alert-error mb-3 py-2 text-sm">{{ actionError }}</div>
    <div v-if="error" class="alert alert-error mb-3 py-2 text-sm">{{ error }}</div>

    <!-- 分段控件：与 StrategyView 的模式切换保持同一套语言 -->
    <div class="bg-base-content/5 mb-3 inline-flex gap-0.5 rounded-lg p-0.5">
      <button
        v-for="item in tabs"
        :key="item.key"
        type="button"
        class="rounded-md px-3 py-1 text-xs transition-colors"
        :class="tab === item.key ? 'tint-neutral font-medium' : 'ink-subtle hover:text-base-content'"
        @click="switchTab(item.key)"
      >
        {{ item.label }}
        <span
          v-if="item.key === 'pending' && admin.counts.pending"
          class="bg-error/15 ink-error ml-1 rounded px-1 text-[10px] tabular-nums"
        >
          {{ admin.counts.pending }}
        </span>
      </button>
    </div>

    <!-- 骨架屏 -->
    <div v-if="loading" class="surface space-y-2 p-4">
      <div v-for="n in 5" :key="n" class="skeleton h-9 w-full" />
    </div>

    <div v-else-if="!users.length" class="surface py-16 text-center">
      <p class="ink-muted">
        {{ tab === 'pending' ? '当前没有待审批的注册申请。' : '这个分组下没有账号。' }}
      </p>
    </div>

    <div v-else class="surface overflow-x-auto">
      <!-- 去掉 table-zebra：极简风用细下边框 + 行悬停 -->
      <table class="table table-sm">
        <thead>
          <tr class="border-base-content/10 border-b">
            <th class="text-[11px] font-medium ink-muted">用户名</th>
            <th class="text-[11px] font-medium ink-muted">显示名</th>
            <th class="text-[11px] font-medium ink-muted">角色</th>
            <th class="text-[11px] font-medium ink-muted">状态</th>
            <th class="text-[11px] font-medium ink-muted">注册时间</th>
            <th class="text-right text-[11px] font-medium ink-muted">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="user in users"
            :key="user.username"
            class="border-base-content/5 hover:bg-base-content/[0.03] border-b transition-colors"
          >
            <td class="text-[13px] font-medium">
              {{ user.username }}
              <span
                v-if="user.username === adminUsername"
                class="tint-primary text-primary ml-1 rounded px-1 text-[10px]"
              >
                root
              </span>
            </td>
            <td class="text-[13px]">{{ user.display_name }}</td>
            <td>
              <span
                class="rounded px-1.5 py-px text-[11px]"
                :class="user.role === 'admin' ? 'tint-primary text-primary' : 'tint-neutral'"
              >
                {{ user.role === 'admin' ? '管理员' : '普通用户' }}
              </span>
            </td>
            <td>
              <span
                class="rounded px-1.5 py-px text-[11px]"
                :class="{
                  'tint-warning ink-warning': user.status === 'pending',
                  'tint-success ink-success': user.status === 'approved',
                  'tint-error ink-error': user.status === 'rejected',
                }"
              >
                {{ user.status === 'pending' ? '待审批' : user.status === 'approved' ? '已通过' : '已拒绝' }}
              </span>
            </td>
            <td class="text-xs ink-muted">{{ user.created_at }}</td>
            <td class="text-right">
              <div class="flex flex-wrap justify-end gap-1">
                <template v-if="user.status === 'pending'">
                  <button
                    type="button"
                    class="btn btn-success btn-xs"
                    :disabled="busyUser === user.username"
                    @click="review(user.username, 'approved')"
                  >
                    通过
                  </button>
                  <button
                    type="button"
                    class="btn btn-ghost btn-xs ink-error"
                    :disabled="busyUser === user.username"
                    @click="review(user.username, 'rejected')"
                  >
                    拒绝
                  </button>
                </template>

                <template v-else>
                  <button
                    type="button"
                    class="btn btn-ghost btn-xs"
                    :disabled="busyUser === user.username"
                    @click="review(user.username, 'pending')"
                  >
                    重置为待审批
                  </button>
                  <button
                    v-if="user.status === 'approved' && allowPromotion"
                    type="button"
                    class="btn btn-ghost btn-xs"
                    :disabled="busyUser === user.username"
                    @click="toggleRole(user)"
                  >
                    {{ user.role === 'admin' ? '降为普通用户' : '设为管理员' }}
                  </button>
                  <button
                    type="button"
                    class="btn btn-ghost btn-xs ink-error"
                    :disabled="busyUser === user.username"
                    @click="removeUser(user)"
                  >
                    删除
                  </button>
                </template>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <p class="mt-3 text-[11px] ink-subtle">
      提示：被拒绝的账号不会被删除，只置为「已拒绝」——对方重新注册时可以复用同一行，
      不会出现「同名再也注册不了」。
      <template v-if="!allowPromotion">
        当前配置（ALLOW_ADMIN_PROMOTION=false）不允许提升管理员，系统中只有 root 一个管理员。
      </template>
    </p>
  </AdminLayout>
</template>
