import { PropsWithChildren } from 'react'
import Taro, { useLaunch } from '@tarojs/taro'
import { CLOUD_ENV } from './utils/config'
import './app.scss'

function App({ children }: PropsWithChildren<any>) {
  useLaunch(() => {
    // 云开发环境 id 统一挂在 globalData.env，云函数/云数据库调用共用同一份
    const app = Taro.getApp()
    app.globalData = { ...app.globalData, env: CLOUD_ENV }

    if (!Taro.cloud) {
      console.error('当前基础库不支持云开发，请使用 2.2.3 或以上版本')
      return
    }
    Taro.cloud.init({
      env: CLOUD_ENV,
      traceUser: true,
    })
  })

  // 登录入口统一在 pages/login 登录门页（首次进入必须登录并绑定手机号）；
  // 这里不做静默自动登录，避免绕过登录页直接进入
  return children
}

export default App
