import type { UserConfigExport } from '@tarojs/cli'

// 生产配置
export default {
  mini: {},
  h5: {
    publicPath: '/',
  },
} satisfies UserConfigExport
