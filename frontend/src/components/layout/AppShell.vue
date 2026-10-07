<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { systemApi } from '@/api'
import type { HealthResult } from '@/api/types'
import ThemeToggle from './ThemeToggle.vue'
import WallpaperPicker from './WallpaperPicker.vue'
import WallpaperBackdrop from './WallpaperBackdrop.vue'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const health = ref<HealthResult | null>(null)
const healthDismissed = ref(false)

/**
 * 降级提示的文案。
 *
 * **不能无条件显示 ``embedding.detail``**：降级可能由嵌入服务、数据库、
 * 未配置 LLM_API_KEY 或 Redis 中的任意一个引起。固定显示嵌入那一句，
 * 会让"其实是 Redis 连不上"被误报成嵌入问题，把人往错的方向引。
 * 所以按实际失败的项挑第一条。
 */
const degradedReason = computed(() => {
  const h = health.value
  if (!h || h.status !== 'degraded') return ''
  if (!h.embedding.ok) return h.embedding.detail
  if (!h.postgres.ok) return h.postgres.detail
  if (!h.llm.ok) return h.llm.detail
  if (!h.redis.ok) return h.redis.detail
  // 各项都 ok 但状态仍是 degraded —— 典型是 hash 伪嵌入
  return h.embedding.detail
})

/** 导航项。管理后台按前缀匹配（它有多个子页），其余精确匹配。 */
const navItems = computed(() => {
  const items = [
    { to: '/strategy', label: '阵容与攻略', prefix: false },
    { to: '/chat', label: '对话', prefix: false },
    { to: '/knowledge', label: '知识库', prefix: false },
  ]
  if (auth.isAdmin) items.push({ to: '/admin/users', label: '管理后台', prefix: true })
  return items
})

function isActive(item: { to: string; prefix: boolean }): boolean {
  if (item.prefix) return route.path.startsWith('/admin')
  return route.path === item.to
}

onMounted(async () => {
  try {
    health.value = await systemApi.health()
  } catch {
    // 健康检查失败不该阻塞界面——它本身就是"用来提示问题"的
  }
})

async function logout(): Promise<void> {
  // 先等服务端吊销完成再跳转：否则组件可能在请求发出前就卸载，
  // 令牌没被吊销、用户却以为已经登出。
  await auth.logout()
  router.push('/login')
}
</script>

<template>
  <!-- 壁纸放在最外层，fixed 定位：不参与文档流，内层怎么滚动都不会带着它动。

       注意 .wallpaper 的 z-index 是 0，而下面的内容容器有 z-10——
       必须显式抬起来，否则 fixed 层会盖住内容。

       ★ 内容区**不整体做玻璃**：那样等于又回到"一整块全屏模糊"，
       壁纸照样被糊掉。毛玻璃由各结构元素自己做——
       顶栏是 .glass-chrome，各页的侧栏/内容区各自带 .glass-region。 -->
  <div class="relative flex h-full flex-col">
    <WallpaperBackdrop />

    <div class="relative z-10 flex min-h-0 flex-1 flex-col">
      <!-- 顶栏：glass-chrome（= 结构层玻璃）。
           它是 sticky 的，正文会从下面滚过去，所以必须真的模糊，
           否则文字会清晰地穿过顶栏。 -->
      <header
        class="glass-chrome sticky top-0 z-20 border-b"
        style="border-color: var(--hairline); height: var(--shell-header-h)"
      >
      <div class="flex h-full items-center gap-4 px-3 sm:px-4">
        <div class="flex shrink-0 items-center gap-2">
          <div
            class="bg-primary text-primary-content flex h-7 w-7 items-center justify-center rounded-lg text-sm font-bold"
          >
            洛
          </div>
          <span class="hidden font-semibold sm:inline">洛克王国攻略</span>
        </div>

        <!-- 导航：选中态用**下划线指示条**，而不是 btn-active 的填充块。
             极简风里"当前在哪一页"靠一条细线表达即可，填充块太重。 -->
        <nav class="flex h-full items-stretch gap-1">
          <RouterLink
            v-for="item in navItems"
            :key="item.to"
            :to="item.to"
            class="relative flex items-center px-2.5 text-sm transition-colors sm:px-3"
            :class="
              isActive(item)
                ? 'text-base-content font-medium'
                : 'text-base-content/70 hover:text-base-content'
            "
          >
            {{ item.label }}
            <span
              v-if="isActive(item)"
              class="bg-primary absolute inset-x-2 bottom-0 h-0.5 rounded-full sm:inset-x-3"
            />
          </RouterLink>
        </nav>

        <div class="ml-auto flex items-center gap-2">
          <WallpaperPicker />
          <ThemeToggle />
          <div class="dropdown dropdown-end">
            <button type="button" tabindex="0" class="btn btn-ghost btn-sm gap-2">
              <span class="hidden sm:inline">{{ auth.displayName }}</span>
              <span
                v-if="auth.isAdmin"
                class="tint-primary text-primary rounded px-1 text-[10px] font-medium"
                title="管理员（root）：可管理全部文档与用户"
              >
                root
              </span>
            </button>
            <!-- 菜单浮在正文之上，用 glass-panel：它背后是变化的文字，
                 只叠半透明色会"字透字"，两边都读不清。 -->
            <ul
              tabindex="0"
              class="dropdown-content menu glass-panel rounded-box z-30 mt-1 w-48 p-2 shadow-lg"
            >
              <li class="menu-title text-xs">{{ auth.user?.username }}</li>
              <li><button type="button" @click="logout">退出登录</button></li>
            </ul>
          </div>
        </div>
      </div>

      <!-- 降级提示：任一项依赖异常时都要让用户知道（文案按实际原因取） -->
      <div
        v-if="degradedReason && !healthDismissed"
        class="tint-warning flex items-center gap-2 px-3 py-1.5 text-xs ink-warning"
      >
        <span class="flex-1">{{ degradedReason }}</span>
        <button type="button" class="btn btn-ghost btn-xs" @click="healthDismissed = true">
          知道了
        </button>
      </div>
    </header>

      <main class="min-h-0 flex-1">
        <slot />
      </main>
    </div>
  </div>
</template>
