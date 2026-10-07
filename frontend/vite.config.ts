import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  // Tailwind CSS 4 走官方 Vite 插件（不再需要 postcss.config）
  plugins: [tailwindcss(), vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    watch: {
      // ★ 必须忽略原子写入的临时目录/文件。
      //
      // 编辑器与工具（含本项目的自动化改写）保存文件时常常是
      // "写临时文件 → rename"：临时目录形如
      //   src/stores/.chat.ts.8656.<uuid>.tmpdir/chat.ts.tmp
      // 它们就落在 src/ 里面。Vite 的 chokidar 会去 watch 这些文件，
      // 而此时写入方往往还持有句柄 → 抛
      //   EBUSY: resource busy or locked, watch '...tmpdir/...tmp'
      // 这个异常**不在 watcher 的容错范围内**，会直接
      // 让整个 vite 进程退出（不是打条日志继续跑）。
      //
      // 症状：改几次代码后 dev server 莫名消失，页面 ERR_CONNECTION_REFUSED，
      // 而且日志里那行 EBUSY 很容易被当成"偶发的文件锁"而忽略。
      ignored: ['**/*.tmpdir/**', '**/.*.tmpdir/**', '**/*.tmp', '**/*.swp', '**/*~'],
    },
    proxy: {
      // 开发期把 /api 转发到后端，前端代码里一律用相对路径 /api/v1/...，
      // 于是本地不需要 CORS、生产也不需要在代码里写死后端地址。
      //
      // SSE 不缓冲由后端响应头 X-Accel-Buffering: no 保证，
      // 这里不需要再额外改写代理响应头。
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
