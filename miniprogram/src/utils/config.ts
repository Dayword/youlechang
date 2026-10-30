// ============================================================
// 后端地址配置
// API_BASE 由 config/index.ts 的 defineConstants 在编译期注入（小程序运行时无 process）
// 改后端地址：改 config/index.ts 里的默认值，或启动编译前设置环境变量 TARO_APP_API_BASE
// 开发期：微信开发者工具勾选"不校验合法域名"；上线期：改为 https 备案域名并在微信后台配置合法域名
// ============================================================
declare const TARO_APP_API_BASE: string

export const API_BASE: string = TARO_APP_API_BASE || 'http://127.0.0.1:8000'

// 微信云开发环境 ID（微信开发者工具「云开发」控制台里创建的环境）
export const CLOUD_ENV = 'cloud1-d0gdsi3zzc1107247'

// 园区固定信息（与后端 map_agent 保持一致）
export const PARK_NAME = '大黄鸭室外游乐园'
export const PARK_ADDRESS = '北京市怀柔区万达广场2号门'
export const PARK_LATITUDE = 40.317475
export const PARK_LONGITUDE = 116.630026
export const PARK_PHONE = '15032030998'

// 跨页自动提问：其他页面写入待发问题，chat 页 useDidShow 读取后自动发送
export const PENDING_QUESTION_KEY = 'pending_question'
