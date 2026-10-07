import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { setUnauthorizedHandler } from './api/http'
import { useAuthStore } from './stores/auth'

import './style.css'
// highlight.js 的主题：代码块的配色。两套分别对应浅色/深色。
import 'highlight.js/styles/github.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)

// 401 统一处理放在这里而不是 http.ts 内部：api 层不该依赖 router / store，
// 否则会形成循环引用（store -> api -> store）。
setUnauthorizedHandler(() => {
  const auth = useAuthStore()
  // **用 clearSession 而不是 logout**：401 说明令牌已经失效，
  // 再调 /auth/logout 只会又拿一个 401，而 401 又触发本回调 —— 死循环。
  auth.clearSession()
  if (router.currentRoute.value.name !== 'login') {
    router.push({ path: '/login', query: { redirect: router.currentRoute.value.fullPath } })
  }
})

app.mount('#app')
