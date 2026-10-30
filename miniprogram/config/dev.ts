import type { UserConfigExport } from '@tarojs/cli'

// 开发配置：本地后端（微信开发者工具需勾选"不校验合法域名"）
export default {
  logger: {
    quiet: false,
    stats: true,
  },
  mini: {},
  h5: {},
} satisfies UserConfigExport
