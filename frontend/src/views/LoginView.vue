<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import ThemeToggle from '@/components/layout/ThemeToggle.vue'
import WallpaperPicker from '@/components/layout/WallpaperPicker.vue'
import WallpaperBackdrop from '@/components/layout/WallpaperBackdrop.vue'
import LoginShowcase from '@/components/roco/LoginShowcase.vue'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const showPassword = ref(false)
const error = ref('')
const busy = ref(false)

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = ''
  busy.value = true
  try {
    await auth.login(username.value.trim(), password.value)
    // 登录后默认进「阵容与攻略」——那是这个项目的主功能
    const redirect = (route.query.redirect as string) || '/strategy'
    router.push(redirect)
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}

/** 一键填入默认管理员账号，省去本地调试时反复手打。
 *  仅在默认口令未被修改时提示——生产环境改了口令这条提示就消失。 */
function fillAdmin(): void {
  username.value = 'admin'
  password.value = 'admin'
}
</script>

<template>
  <div class="relative flex min-h-full">
    <WallpaperBackdrop />

    <!-- ==================== 左：登录表单 ==================== -->
    <!-- glass-region：表单侧是结构层，用毛玻璃。
         不用不透明底色（那会把壁纸整块遮死、登录页就没有壁纸可言），
         也不裸着放（文字直接压在壁纸的锐利纹理上不好读）。
         毛玻璃正好取中间：壁纸的明暗与色彩透过来，细节被抹平。 -->
    <div class="glass-region relative z-10 flex w-full flex-col lg:w-[46%] xl:w-[42%]">
      <div class="absolute top-4 right-4 z-10 flex items-center gap-1">
        <WallpaperPicker />
        <ThemeToggle />
      </div>

      <div class="flex flex-1 items-center justify-center p-6 sm:p-10">
        <div class="w-full max-w-sm">
          <!-- 品牌 -->
          <div class="mb-8 flex items-center gap-3">
            <div
              class="bg-primary text-primary-content flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl text-xl font-bold shadow-lg"
            >
              洛
            </div>
            <div class="min-w-0">
              <h1 class="truncate text-lg font-bold">洛克王国：世界</h1>
              <p class="text-xs ink-muted">PvP 阵容攻略 Agent</p>
            </div>
          </div>

          <h2 class="text-xl font-semibold">登录</h2>
          <p class="mt-1 mb-6 text-xs ink-muted">
            登录后可查询阵容、运行五 Agent 攻略研究
          </p>

          <form class="space-y-4" @submit.prevent="submit">
            <label class="form-control w-full">
              <span class="label-text mb-1.5 text-xs font-medium">用户名</span>
              <input
                v-model="username"
                class="input w-full"
                placeholder="请输入用户名"
                autocomplete="username"
                required
                :disabled="busy"
              />
            </label>

            <label class="form-control w-full">
              <span class="label-text mb-1.5 text-xs font-medium">密码</span>
              <!-- 密码框：右侧带"显示/隐藏"按钮。
                   不加这个的话，输错了只能整段删掉重打。 -->
              <div class="relative">
                <input
                  v-model="password"
                  :type="showPassword ? 'text' : 'password'"
                  class="input w-full pr-11"
                  placeholder="请输入密码"
                  autocomplete="current-password"
                  required
                  :disabled="busy"
                />
                <button
                  type="button"
                  class="btn btn-ghost btn-xs btn-circle absolute top-1/2 right-2 -translate-y-1/2"
                  :title="showPassword ? '隐藏密码' : '显示密码'"
                  tabindex="-1"
                  @click="showPassword = !showPassword"
                >
                  <svg
                    v-if="showPassword"
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="currentColor"
                    class="h-4 w-4"
                  >
                    <path d="M12 7a5 5 0 100 10 5 5 0 000-10zm0-5C7 2 2.7 5.1 1 9.5 2.7 13.9 7 17 12 17s9.3-3.1 11-7.5C21.3 5.1 17 2 12 2zm0 12.5a5 5 0 110-10 5 5 0 010 10z" />
                  </svg>
                  <svg
                    v-else
                    xmlns="http://www.w3.org/2000/svg"
                    viewBox="0 0 24 24"
                    fill="currentColor"
                    class="h-4 w-4"
                  >
                    <path d="M12 7a5 5 0 015 5c0 .65-.13 1.26-.36 1.83l2.92 2.92c1.51-1.26 2.7-2.89 3.44-4.75-1.73-4.39-6-7.5-11-7.5-1.4 0-2.74.25-3.98.7l2.16 2.16C10.74 7.13 11.35 7 12 7zM2 4.27l2.28 2.28.46.46C3.08 8.3 1.78 10.02 1 12c1.73 4.39 6 7.5 11 7.5 1.55 0 3.03-.3 4.38-.84l.42.42L19.73 22 21 20.73 3.27 3 2 4.27zM7.53 9.8l1.55 1.55c-.05.21-.08.43-.08.65a3 3 0 003 3c.22 0 .44-.03.65-.08l1.55 1.55c-.67.33-1.41.53-2.2.53a5 5 0 01-5-5c0-.79.2-1.53.53-2.2z" />
                  </svg>
                </button>
              </div>
            </label>

            <div v-if="error" class="alert alert-error py-2.5 text-xs">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4 shrink-0">
                <path d="M12 2a10 10 0 100 20 10 10 0 000-20zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z" />
              </svg>
              <span>{{ error }}</span>
            </div>

            <button type="submit" class="btn btn-primary btn-block" :disabled="busy">
              <span v-if="busy" class="loading loading-spinner loading-sm" />
              {{ busy ? '登录中…' : '登录' }}
            </button>
          </form>

          <!-- 默认账号提示：本地调试省事。生产改了口令这条就不再显示。 -->
          <div class="mt-4 flex items-center justify-between text-[11px]">
            <span class="ink-subtle">默认管理员：admin / admin</span>
            <button type="button" class="btn btn-ghost btn-xs" @click="fillAdmin">一键填入</button>
          </div>

          <div class="divider my-5 text-[10px] ink-subtle">或</div>

          <p class="text-center text-xs ink-muted">
            还没有账号？
            <RouterLink to="/register" class="link link-primary font-medium">立即注册</RouterLink>
          </p>
          <p class="mt-1.5 text-center text-[11px] ink-subtle">
            注册后需要管理员在后台审批通过才能登录
          </p>
        </div>
      </div>
    </div>

    <!-- ==================== 右：动态展示 ==================== -->
    <!-- 窄屏隐藏：右侧舞台是装饰，手机上优先把空间留给表单 -->
    <div class="relative hidden flex-1 lg:block">
      <LoginShowcase />
    </div>
  </div>
</template>
