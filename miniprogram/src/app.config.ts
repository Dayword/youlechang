// ============================================================
// 小程序全局配置：三 Tab（首页 | AI问答居中 | 我的）
// ============================================================
export default defineAppConfig({
  pages: [
    'pages/index/index',
    'pages/login/index',
    'pages/chat/index',
    'pages/mine/index',
    'pages/recharge/index',
    'pages/passes/index',
  ],
  tabBar: {
    color: '#999999',
    selectedColor: '#FF8C42',
    backgroundColor: '#ffffff',
    borderStyle: 'white',
    // AI 问答固定在中间 Tab（图标为本地 assets/tabbar/*.png，81x81）
    list: [
      {
        pagePath: 'pages/index/index',
        text: '首页',
        iconPath: 'assets/tabbar/home.png',
        selectedIconPath: 'assets/tabbar/home-active.png',
      },
      {
        pagePath: 'pages/chat/index',
        text: 'AI问答',
        iconPath: 'assets/tabbar/chat.png',
        selectedIconPath: 'assets/tabbar/chat-active.png',
      },
      {
        pagePath: 'pages/mine/index',
        text: '我的',
        iconPath: 'assets/tabbar/mine.png',
        selectedIconPath: 'assets/tabbar/mine-active.png',
      },
    ],
  },
  window: {
    backgroundTextStyle: 'light',
    navigationBarBackgroundColor: '#FFF7E0',
    navigationBarTitleText: '鸭先知',
    navigationBarTextStyle: 'black',
  },
  // 微信同声传译插件（语音转文字）：需先在小程序后台添加授权后才能声明！
  // 添加路径：mp.weixin.qq.com 登录本小程序 → 「设置」→「第三方设置」→「插件管理」
  //   → 搜索「同声传译」（provider 微信智聆）→ 添加（个人主体可用，即时生效）
  // ⚠️ 未添加插件时保留声明会导致小程序启动报错「插件未授权使用」
  // 添加完成后，取消下方注释并重新编译即可启用语音输入：
  // plugins: {
  //   WechatSI: {
  //     version: '0.3.5',
  //     provider: 'wx069ba97219f66d99',
  //   },
  // },
})
