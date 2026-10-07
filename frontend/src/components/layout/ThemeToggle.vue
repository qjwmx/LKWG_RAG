<script setup lang="ts">
import { useTheme } from '@/composables/useTheme'

const { theme, toggle } = useTheme()
</script>

<template>
  <button
    type="button"
    class="btn btn-ghost btn-sm btn-square"
    :title="theme === 'business' ? '切换到浅色主题' : '切换到深色主题'"
    @click="toggle"
  >
    <!-- 图标 h-5 w-5（原来是 h-4 w-4）：与壁纸按钮并排时大小要一致，
         否则顶栏右侧会显得参差。 -->
    <svg v-if="theme === 'business'" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-5 w-5">
      <path d="M12 7a5 5 0 100 10 5 5 0 000-10zm0-5a1 1 0 011 1v2a1 1 0 11-2 0V3a1 1 0 011-1zm0 16a1 1 0 011 1v2a1 1 0 11-2 0v-2a1 1 0 011-1zM4 12a1 1 0 011-1h2a1 1 0 110 2H5a1 1 0 01-1-1zm14 0a1 1 0 011-1h2a1 1 0 110 2h-2a1 1 0 01-1-1zM6.3 6.3a1 1 0 011.4 0l1.4 1.4a1 1 0 11-1.4 1.4L6.3 7.7a1 1 0 010-1.4zm9.6 9.6a1 1 0 011.4 0l1.4 1.4a1 1 0 11-1.4 1.4l-1.4-1.4a1 1 0 010-1.4zm2.8-9.6a1 1 0 010 1.4l-1.4 1.4a1 1 0 11-1.4-1.4l1.4-1.4a1 1 0 011.4 0zM7.7 16.3a1 1 0 010 1.4l-1.4 1.4a1 1 0 11-1.4-1.4l1.4-1.4a1 1 0 011.4 0z" />
    </svg>
    <svg v-else xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-5 w-5">
      <path d="M21.64 13a1 1 0 00-1.05-.14 8.05 8.05 0 01-3.37.73 8.15 8.15 0 01-8.14-8.1 8.6 8.6 0 01.25-2A1 1 0 008 2.36a10.14 10.14 0 1014 11.69 1 1 0 00-.36-1.05z" />
    </svg>
  </button>
</template>
