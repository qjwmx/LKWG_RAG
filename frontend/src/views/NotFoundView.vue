<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <!-- 404 页也要有壁纸与玻璃，否则从别的页面跳进来会像"换了个网站"。
       bg-base-200 是不透明的，会把壁纸完全盖住，改成玻璃结构层。 -->
  <div class="glass-region relative flex min-h-full flex-col items-center justify-center gap-4 p-4">
    <p class="text-6xl font-bold opacity-20">404</p>
    <p class="ink-muted">页面不存在。</p>
    <div class="flex gap-2">
      <button type="button" class="btn btn-primary btn-sm" @click="router.push('/chat')">
        去问答
      </button>
      <button type="button" class="btn btn-ghost btn-sm" @click="router.back()">返回上一页</button>
    </div>
  </div>
</template>
