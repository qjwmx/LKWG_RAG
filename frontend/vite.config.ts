import { fileURLToPath, URL } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig(({ mode }) => {
  // 读 .env / .env.local / 环境变量。**只读 VITE_ 前缀**，避免把后端密钥
  // （LLM_API_KEY 等）误打进前端产物。
  const env = loadEnv(mode, process.cwd(), 'VITE_')

  // 开发代理的后端地址。默认本机 8000，前后端不在同一台机器时用它改。
  // 例：VITE_API_TARGET=http://10.0.0.5:8000 npm run dev
  const apiTarget = env.VITE_API_TARGET || 'http://127.0.0.1:8000'

  // 开发服务器监听地址。默认只监听回环（安全）。
  // 要在局域网/远程浏览器里访问开发服务器时：
  //   VITE_DEV_HOST=0.0.0.0 npm run dev
  // 生产部署不需要它——生产是构建成静态文件后由 Nginx 或后端托管。
  const devHost = env.VITE_DEV_HOST || '127.0.0.1'
  const devPort = Number(env.VITE_DEV_PORT || 5173)

  return {
    // Tailwind CSS 4 走官方 Vite 插件（不再需要 postcss.config）
    plugins: [tailwindcss(), vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      host: devHost,
      port: devPort,
      // 端口被占用时直接失败，而不是悄悄换一个端口——
      // 换了端口后浏览器还开着旧地址，现象是"页面白屏/连不上"，很难归因。
      strictPort: true,
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
          target: apiTarget,
          changeOrigin: true,
        },
      },
    },
  }
})
