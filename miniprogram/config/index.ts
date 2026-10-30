import { defineConfig, type UserConfigExport } from '@tarojs/cli'

import devConfig from './dev'
import prodConfig from './prod'

export default defineConfig(async (merge, { command, mode }) => {
  const baseConfig: UserConfigExport = {
    projectName: 'amusement-park-mp',
    date: '2026-8-29',
    designWidth: 750,
    deviceRatio: {
      640: 2.34 / 2,
      750: 1,
      375: 2,
      828: 1.81 / 2,
    },
    sourceRoot: 'src',
    outputRoot: 'dist',
    plugins: [],
    defineConstants: {
      // 编译期注入全局常量（小程序运行时无 process 对象，禁止在源码里直接用 process.env）
      //
      // 【首选】双击项目根目录的「小程序重新编译.bat」：
      //   它会自动探测本机 WLAN 的 IPv4 并注入 TARO_APP_API_BASE —— 换 Wi-Fi / 重启电脑
      //   都不用再手改任何 IP。
      //
      // 【手改兜底】直接跑 npm run build:weapp（不带环境变量）时用下面这个默认值。
      //   默认 127.0.0.1 只够「开发者工具模拟器」用；真机预览必须换成电脑的局域网 IP：
      //   1) 电脑连 Wi-Fi（WLAN 网卡）时用 ipconfig 查「WLAN 适配器」的 IPv4（注意：
      //      192.168.233.1 / 192.168.127.1 是 VMware 虚拟网卡，手机路由不到，不要用）
      //   2) 手机与电脑连同一个 Wi-Fi
      //   3) 防火墙放行 8000 端口（netsh advfirewall firewall add rule ...）
      //   4) 开发者工具「详情→本地设置」勾选「不校验合法域名」；手机预览时右上角
      //      「…→开发调试→打开调试」跳过域名校验
      TARO_APP_API_BASE: JSON.stringify(
        process.env.TARO_APP_API_BASE || 'http://10.116.25.206:8000'
      ),
    },
    copy: {
      patterns: [],
      options: {},
    },
    framework: 'react',
    compiler: 'webpack5',
    cache: { enable: true },
    mini: {
      postcss: {
        pxtransform: {
          enable: true,
          config: {},
        },
        cssModules: {
          enable: false,
        },
      },
      // SSE 分块接收需要基础库 2.20.1+；语音转文字需要同声传译插件
      webpackChain(chain) {
        chain.resolve.alias.set('@', `${process.cwd()}/src`)
      },
    },
    h5: {},
  }

  if (process.env.NODE_ENV === 'development') {
    return merge({}, baseConfig, devConfig)
  }
  return merge({}, baseConfig, prodConfig)
})
