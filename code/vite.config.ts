import path from 'path'
import { defineConfig } from '@lark-apaas/coding-preset-vite-react'

// 前端 dev server 端口。预设默认是 8080 + strictPort=true —— 8080 一旦被占用，
// vite 会直接退出进程，表现就是「脚本启动了但浏览器打不开」。
// 这里固定用 Vite 惯例的 5173，并允许端口被占时自动顺延（不再崩掉）。
// 需要换端口：CLIENT_DEV_PORT=xxxx npm run dev
const DEV_PORT = Number(process.env.CLIENT_DEV_PORT) || 5173

export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
      '@shared': path.resolve(__dirname, 'shared'),
    },
  },
  server: {
    // 不绑死 localhost：预设默认绑 ::1，浏览器/预览面板走 127.0.0.1 时会连不上。
    // true = 同时监听 IPv4 + IPv6 全部网卡，本机、局域网、内置预览面板都能进。
    host: process.env.CLIENT_DEV_HOST || true,
    port: DEV_PORT,
    strictPort: false,
  },
})
