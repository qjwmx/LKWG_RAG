<script setup lang="ts">
import { computed, ref } from 'vue'
import { useAuthStore } from '@/stores/auth'
import ThemeToggle from '@/components/layout/ThemeToggle.vue'
import WallpaperPicker from '@/components/layout/WallpaperPicker.vue'
import WallpaperBackdrop from '@/components/layout/WallpaperBackdrop.vue'
import LoginShowcase from '@/components/roco/LoginShowcase.vue'

const auth = useAuthStore()

const username = ref('')
const displayName = ref('')
const password = ref('')
const confirm = ref('')
const error = ref('')
const success = ref('')
const busy = ref(false)

/** 两次密码是否一致。只在确认框有内容时才提示，避免刚进页面就报红。 */
const mismatch = computed(
  () => confirm.value.length > 0 && password.value !== confirm.value,
)

async function submit(): Promise<void> {
  if (busy.value) return
  error.value = ''
  success.value = ''

  if (password.value !== confirm.value) {
    error.value = '两次输入的密码不一致。'
    return
  }

  busy.value = true
  try {
    await auth.register(username.value.trim(), password.value, displayName.value.trim())
    success.value = '注册申请已提交。请等待管理员在管理后台「用户审批」中通过后再登录。'
    username.value = ''
    displayName.value = ''
    password.value = ''
    confirm.value = ''
  } catch (exc) {
    error.value = exc instanceof Error ? exc.message : String(exc)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="relative flex min-h-full">
    <WallpaperBackdrop />

    <!-- 左侧表单，与登录页保持同一套布局，来回切换不会"跳版"。
         glass-region：毛玻璃结构层，理由同 LoginView。 -->
    <div class="glass-region relative z-10 flex w-full flex-col lg:w-[46%] xl:w-[42%]">
      <div class="absolute top-4 right-4 z-10 flex items-center gap-1">
        <WallpaperPicker />
        <ThemeToggle />
      </div>

      <div class="flex flex-1 items-center justify-center p-6 sm:p-10">
        <div class="w-full max-w-sm">
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

          <h2 class="text-xl font-semibold">注册账号</h2>
          <p class="mt-1 mb-6 text-xs ink-muted">
            提交后需要管理员审批。通过之前无法登录——这是刻意设计的闸门。
          </p>

          <form class="space-y-3.5" @submit.prevent="submit">
            <label class="form-control w-full">
              <span class="label-text mb-1.5 text-xs font-medium">用户名</span>
              <input
                v-model="username"
                class="input w-full"
                placeholder="3–32 个字符"
                autocomplete="username"
                required
                minlength="3"
                :disabled="busy"
              />
              <span class="mt-1 text-[11px] ink-subtle">不能包含空格</span>
            </label>

            <label class="form-control w-full">
              <span class="label-text mb-1.5 text-xs font-medium">显示名（可选）</span>
              <input
                v-model="displayName"
                class="input w-full"
                placeholder="留空则用用户名"
                :disabled="busy"
              />
            </label>

            <label class="form-control w-full">
              <span class="label-text mb-1.5 text-xs font-medium">密码</span>
              <input
                v-model="password"
                type="password"
                class="input w-full"
                placeholder="至少 6 位"
                autocomplete="new-password"
                required
                minlength="6"
                :disabled="busy"
              />
            </label>

            <label class="form-control w-full">
              <span class="label-text mb-1.5 text-xs font-medium">确认密码</span>
              <input
                v-model="confirm"
                type="password"
                class="input w-full"
                :class="mismatch ? 'input-error' : ''"
                autocomplete="new-password"
                required
                :disabled="busy"
              />
              <!-- 实时提示不一致：等到提交才报错会让人白填一遍 -->
              <span v-if="mismatch" class="mt-1 text-[11px] ink-error">
                两次输入的密码不一致
              </span>
            </label>

            <div v-if="error" class="alert alert-error py-2.5 text-xs">
              <span>{{ error }}</span>
            </div>
            <div v-if="success" class="alert alert-success py-2.5 text-xs">
              <span>{{ success }}</span>
            </div>

            <button type="submit" class="btn btn-primary btn-block" :disabled="busy || mismatch">
              <span v-if="busy" class="loading loading-spinner loading-sm" />
              {{ busy ? '提交中…' : '提交注册申请' }}
            </button>
          </form>

          <div class="divider my-5 text-[10px] ink-subtle">或</div>

          <p class="text-center text-xs ink-muted">
            已有账号？
            <RouterLink to="/login" class="link link-primary font-medium">返回登录</RouterLink>
          </p>
        </div>
      </div>
    </div>

    <!-- 右侧动画与登录页一致 -->
    <div class="relative hidden flex-1 lg:block">
      <LoginShowcase />
    </div>
  </div>
</template>
