<script setup lang="ts">
import { onBeforeUnmount, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useAdminStore } from '@/stores/admin'
import AppShell from './AppShell.vue'

const admin = useAdminStore()
const route = useRoute()

const links = [
  { to: '/admin/users', label: '用户审批', icon: 'M16 11c1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3 1.34 3 3 3zm-8 0c1.66 0 3-1.34 3-3S9.66 5 8 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z' },
  { to: '/admin/docs', label: '文档管理', icon: 'M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z' },
  { to: '/admin/qa', label: '问答与反馈', icon: 'M20 2H4a2 2 0 00-2 2v18l4-4h14a2 2 0 002-2V4a2 2 0 00-2-2z' },
  { to: '/admin/stats', label: '统计', icon: 'M5 9.2h3V19H5V9.2zM10.6 5h2.8v14h-2.8V5zm5.6 8H19v6h-2.8v-6z' },
  // 观测：仅管理员可见。整个 AdminLayout 只出现在 /admin/* 下，
  // 而那些路由全部带 meta.admin，守卫会拦掉非管理员 —— 所以普通用户
  // 既进不到后台，也就看不到这一项。
  { to: '/admin/observability', label: '观测', icon: 'M3 13h4l2.5-7 3 14 2.5-7H21v2h-4.2l-2.3 6.4a1 1 0 01-1.9-.05L10 8.6 8.2 15H3v-2z' },
]

onMounted(() => admin.startPolling())
onBeforeUnmount(() => admin.stopPolling())
</script>

<template>
  <AppShell>
    <div class="flex" style="height: calc(100vh - var(--shell-header-h))">
      <!-- 后台侧栏：结构层，用 glass-region -->
      <nav
        class="glass-region hidden w-52 shrink-0 flex-col border-r p-3 md:flex"
        style="border-color: var(--hairline)"
      >
        <p class="mb-2 px-2 text-[11px] font-medium tracking-wide uppercase ink-subtle">
          管理后台
        </p>
        <ul class="flex flex-col gap-0.5">
          <li v-for="link in links" :key="link.to">
            <RouterLink
              :to="link.to"
              class="hover:bg-base-content/5 relative flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm transition-colors"
              :class="route.path === link.to ? 'bg-base-content/8 font-medium' : 'ink-muted'"
            >
              <!-- 左指示条：极简风的选中态表达 -->
              <span
                v-if="route.path === link.to"
                class="bg-primary absolute top-1.5 bottom-1.5 -left-3 w-0.5 rounded-full"
              />
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
                <path :d="link.icon" />
              </svg>
              <span>{{ link.label }}</span>
              <!-- 待审批角标：任何时候都能看到还有几个账号等着批 -->
              <span
                v-if="link.to === '/admin/users' && admin.pendingCount"
                class="bg-error/15 ink-error ml-auto rounded px-1 text-[10px] font-medium tabular-nums"
              >
                {{ admin.pendingCount }}
              </span>
            </RouterLink>
          </li>
        </ul>

        <div class="mt-auto px-2 pt-4 text-[11px] ink-subtle">
          <p>root：{{ admin.adminUsername }}</p>
          <p class="mt-1">root 可管理全部文档与用户</p>
        </div>
      </nav>

      <!-- 移动端顶部标签 -->
      <div class="glass-region flex min-w-0 flex-1 flex-col">
        <div
          class="flex gap-1 overflow-x-auto border-b p-2 md:hidden"
          style="border-color: var(--hairline)"
        >
          <RouterLink
            v-for="link in links"
            :key="link.to"
            :to="link.to"
            class="shrink-0 rounded-md px-2 py-1 text-xs transition-colors"
            :class="
              route.path === link.to ? 'bg-base-content/8 font-medium' : 'ink-muted'
            "
          >
            {{ link.label }}
            <span
              v-if="link.to === '/admin/users' && admin.pendingCount"
              class="bg-error/15 ink-error ml-1 rounded px-1 text-[10px]"
            >
              {{ admin.pendingCount }}
            </span>
          </RouterLink>
        </div>

        <div class="scrollbar-slim min-h-0 flex-1 overflow-y-auto">
          <div class="mx-auto max-w-6xl p-4">
            <slot />
          </div>
        </div>
      </div>
    </div>
  </AppShell>
</template>
