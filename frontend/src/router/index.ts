import { createRouter, createWebHistory } from 'vue-router'
import { getToken } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', redirect: '/strategy' },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/register',
      name: 'register',
      component: () => import('@/views/RegisterView.vue'),
      meta: { public: true },
    },
    {
      path: '/chat',
      name: 'chat',
      component: () => import('@/views/ChatView.vue'),
    },
    {
      // 洛克王国主入口：阵容查询 + 五 Agent 攻略研究
      path: '/strategy',
      name: 'strategy',
      component: () => import('@/views/StrategyView.vue'),
    },
    {
      path: '/knowledge',
      name: 'knowledge',
      component: () => import('@/views/KnowledgeView.vue'),
    },
    {
      path: '/admin',
      redirect: '/admin/users',
      meta: { admin: true },
    },
    {
      path: '/admin/users',
      name: 'admin-users',
      component: () => import('@/views/admin/AdminUsersView.vue'),
      meta: { admin: true },
    },
    {
      path: '/admin/docs',
      name: 'admin-docs',
      component: () => import('@/views/admin/AdminDocsView.vue'),
      meta: { admin: true },
    },
    {
      path: '/admin/qa',
      name: 'admin-qa',
      component: () => import('@/views/admin/AdminQaView.vue'),
      meta: { admin: true },
    },
    {
      path: '/admin/stats',
      name: 'admin-stats',
      component: () => import('@/views/admin/AdminStatsView.vue'),
      meta: { admin: true },
    },
    {
      // 观测：请求轨迹 / 模型用量 / 缓存状态。**只有管理员能进**——
      // 路由守卫 + 侧栏隐藏，真正的拦截在后端 require_admin（普通用户 403）。
      path: '/admin/observability',
      name: 'admin-observability',
      component: () => import('@/views/admin/AdminObservabilityView.vue'),
      meta: { admin: true },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFoundView.vue'),
      meta: { public: true },
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 首次进入且还没确认过登录态时，先用本地 token 换一次用户信息。
  // 不这么做的话，刷新页面会被误判成未登录而踢回登录页。
  if (!auth.ready) {
    await auth.bootstrap()
  }

  if (to.meta.public) {
    // 已登录用户不该再看到登录页，直接送去主功能
    if (auth.isLoggedIn && (to.name === 'login' || to.name === 'register')) {
      return { path: '/strategy' }
    }
    return true
  }

  if (!auth.isLoggedIn && !getToken()) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }

  if (to.meta.admin && !auth.isAdmin) {
    // 前端守卫只是"不显示无权入口"，真正的拦截在服务端（require_admin -> 403）
    return { path: '/strategy' }
  }

  return true
})

export default router
